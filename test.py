#!/usr/bin/env python
import sys
import os
import re
from bintran import Elf32, Elf32_Rel, Elf32_Sym, Elf32_Shdr
from uuid import uuid4
from ctypes import *

def cfi_2(elf):
    '''fill ret_table'''
    assert elf.ehdr.e_type == 2, 'not an executable?'
    retab = 0
    retabsz = 0
    strtabndx = elf('.symtab').sh_link
    for s in elf('.symtab', Elf32_Sym):
        if elf[elf.shdrs[strtabndx].sh_offset+s.st_name:] == 'ret_table':
            retab = s.st_value
            retabsz = s.st_size
            break
    else:
        assert False, 'ret_table not present?'
    retabndx = 0
    insns = elf.disasm()
    print '  %d CALLs found' % len(filter(lambda i: i.bytes == '\x68\xef\xbe\xad\xde', insns))
    print '  %d RETs found' % len(filter(lambda i: i.bytes == '\xff\x24\x8d\xef\xbe\xad\xde', insns))
    for i in range(len(insns)):
        if insns[i].bytes == '\x68\xef\xbe\xad\xde': # push $0xdeadbeef
            assert retabndx * 4 < retabsz
            elf[elf.addr2off(retab)+retabndx*4:] = string_at(pointer(c_uint(insns[i+1].address+len(insns[i+1]))), 4)
            elf[elf.addr2off(insns[i].address+1):] = string_at(pointer(c_uint(retabndx)), 4)
            retabndx += 1
        elif insns[i].bytes == '\xff\x24\x8d\xef\xbe\xad\xde': # jmp *$0xdeadbeef(,%ecx,4)
            assert insns[i-1].bytes == '\x59' # pop %ecx
            elf[elf.addr2off(insns[i].address+3):] = string_at(pointer(c_uint(retab)), 4)
    return elf

def cfi_1(elf):
    '''replace CALL with PUSH and JMP and RET with POP and JMP'''
    assert elf.ehdr.e_type == 1, 'not a relocatable?'
    # handle CALLs
    calls = filter(lambda i: i.mnemonic == 'call', elf.disasm())
    print '  %d CALLs found' % len(calls)
    calls.reverse()
    for i in calls:
        push = '\x68\xef\xbe\xad\xde' # "push $0xdeadbeef"
        elf.insert(i.address, push)
        text_offset = elf('.text').sh_offset
        if elf[text_offset+i.address+len(push)] == '\xe8': # direct CALL
            elf[text_offset+i.address+len(push)] = '\xe9'
        elif elf[text_offset+i.address+len(push)] == '\xff': # indirect CALL
            elf[text_offset+i.address+len(push)+1] = chr(ord(elf[text_offset+i.address+len(push)+1]) + 0x10)
        else:
            assert False, 'unexpected CALL? %s %s' % (i.mnemonic, i.op_str)
    # handle RETs (REPZ RETs)
    rets = filter(lambda i: i.bytes == '\xc3' or i.bytes == '\xf3\xc3', elf.disasm())
    print '  %d RETs found' % len(rets)
    rets.reverse()
    for r in rets:
        elf.insert(r.address, '\x90' * (8 - len(r))) # need 8 bytes in total
        elf[elf('.text').sh_offset+r.address:] = \
                '\x59\xff\x24\x8d\xef\xbe\xad\xde' # "pop %ecx; jmp *0xdeadbeef(,%ecx,4)"
    return elf

def flatten(elf):
    '''rewrite short "jmp" to near "jmp"'''
    sjs = filter(lambda i: i.mnemonic.startswith('j') and \
            i.op_str[0] != '*' and len(i) == 2, elf.disasm())
    print '  %d short JMPs' % len(sjs)
    elf.flatten()
    return elf

def add_bound_check(elf):
    '''add checks before "jmp/call *table(,%reg,4)"'''
    syms = elf('.symtab', Elf32_Sym)
    insns = elf.disasm()
    insns.reverse()
    for i in insns:
        # parse the instruction
        r = re.search(r'\*(.*)\(,%(.*),4\)', i.op_str)
        if not r:
            continue
        print '  %s' % str(i)
        addend, reg = int(r.group(1), 16), r.group(2)
        # find the relent of table base in the instruction
        r = next((r for r in elf('.rel.text', Elf32_Rel) if r.r_offset == i.address + 3), None)
        assert r, 'relent for jump table is not found'
        # find the symbol associated with the relent
        assert r.r_info & 0xff == 1, 'not an R_386_32 relent?'
        s = syms[r.r_info>>8]
        assert s.st_info & 0xf == 3, 'not a section symbol?'
        # find SHT_REL section for jump table section (e.g., .rodata)
        sh = next((sh for sh in elf.shdrs if sh.sh_type == 9 and sh.sh_info == s.st_shndx), None)
        assert sh, 'SHT_REL for jump table section is not found'
        # count the number of jump table entries
        rels = (sh.sh_size/sizeof(Elf32_Rel) * Elf32_Rel).from_buffer(elf.binary, sh.sh_offset)
        count = 0
        marker = addend
        for r in rels:
            if r.r_offset < marker:
                continue
            if r.r_offset == marker:
                count += 1
                marker += 4
            else:
                break
        # generate instrumentation
        nasm = ('cmp %s,%d\n'
                'jae short $\n') % (reg, count)
        tmpfile = '.%s' % uuid4()
        with open(tmpfile, 'w') as f:
            f.write(nasm)
        os.system('nasm %s -o %s.o' % (tmpfile, tmpfile))
        with open('%s.o' % tmpfile, 'rb') as f:
            payload = f.read()
        os.unlink(tmpfile)
        os.unlink('%s.o' % tmpfile)
        # apply instrumentation
        elf.insert(i.address, payload)
    return elf

def call_to_jmp(elf):
    '''rewrite "call dest" to "push addr; jmp dest"'''
    syms = elf('.symtab', Elf32_Sym)
    # find symbol index of .text section
    tsndx = next((i for i in range(len(syms)) if syms[i].st_info & 0xf == 3 and syms[i].st_shndx == 1), -1)
    assert tsndx >= 0, 'symbol of .text section is not found'
    calls = filter(lambda i: i.mnemonic == 'call', elf.disasm())
    print '  %d CALLs found' % len(calls)
    calls.reverse()
    for i in calls:
        # insert "push addr"
        elf.insert(i.address, '\x68%s' % string_at(pointer(c_uint(i.address+5+len(i))), 4))
        # add a relent for the return address in "push"
        if not elf('.rel.text'):
            symtab_shndx = (addressof(elf('.symtab'))-addressof(elf.shdrs)) / sizeof(Elf32_Shdr)
            elf.add_section('.rel.text', sh_type=9, sh_info=1, sh_entsize=sizeof(Elf32_Rel), sh_link=symtab_shndx)
        elf.add_entry(elf('.rel.text'), Elf32_Rel(i.address+1, (tsndx<<8)+1), lambda r: r.r_offset)
        # rewrite "call" to "jmp"
        call_offset = elf('.text').sh_offset + i.address + 5
        if elf[call_offset] == '\xe8': # direct "call"
            elf[call_offset] = '\xe9'
        elif elf[call_offset] == '\xff': # indirect "call"
            elf[call_offset+1] = chr(ord(elf[call_offset+1]) + 0x10)
        else:
            assert False, 'what is the call? %s' % str(i)
    return elf

def ret_to_jmp(elf):
    '''rewrite "ret" to "pop %ecx; jmp *%ecx"'''
    rets = [i.address for i in filter(lambda i: i.bytes == '\xc3', elf.disasm())]
    print '  %d RETs found' % len(rets)
    rets.reverse()
    for r in rets:
        elf.insert(r, '\x90'*2)
        elf[elf('.text').sh_offset+r:] = '\x59\xff\xe1'
    return elf

def add_nop(elf):
    '''add "nop" between every two instruction'''
    iaddrs = [i.address for i in elf.disasm()]
    print '  %d insns found' % len(iaddrs)
    iaddrs.reverse()
    for ia in iaddrs:
        elf.insert(ia, '\x90')
    return elf

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print ('usage: test.py [option+option+..] xxx.o\n'
               '  flatten: rewrite all short JMPs to near JMPs\n'
               '  add_bound_check: add check before jump table indexing\n'
               '  call_to_jmp: rewrite CALL to PUSH and JMP\n'
               '  ret_to_jmp: rewrite RET to POP and JMP\n'
               '  add_nop: add NOP after every instruction')
        sys.exit(0)
    tests = [globals()[t] for t in sys.argv[1].split('+')]
    for objfile in sys.argv[2:]:
        print '%s' % objfile
        with open(objfile, 'rb') as f:
            elf = Elf32(f.read())
        for t in tests:
            elf = t(elf)
        with open(objfile, 'wb') as f:
            f.write(str(elf))
