"""
Defines PyRTL memories.
These blocks of memories can be read (potentially async) and written (sync)

MemBlocks supports any number of the following operations:

* read: `d = mem[address]`
* write: `mem[address] <<= d`
* write with an enable: `mem[address] <<= MemBlock.EnabledWrite(d,enable=we)`

Based on the number of reads and writes a memory will be inferred
with the correct number of ports to support that
"""

from __future__ import print_function, unicode_literals
import collections

from .pyrtlexceptions import PyrtlError
from .core import working_block, LogicNet, _NameIndexer
from .wire import WireVector, Const, next_tempvar_name
from .corecircuits import as_wires
# ------------------------------------------------------------------------
#
#         ___        __   __          __        __   __
#   |\/| |__   |\/| /  \ |__) \ /    |__) |    /  \ /  ` |__/
#   |  | |___  |  | \__/ |  \  |     |__) |___ \__/ \__, |  \
#


_memIndex = _NameIndexer()

_MemAssignment = collections.namedtuple('_MemAssignment', 'rhs, is_conditional')
"""_MemAssignment is the type returned from assignment by |= or <<="""


def _reset_memory_indexer():
    global _memIndex
    _memIndex = _NameIndexer()


class _MemIndexed(WireVector):
    """ Object used internally to route memory assigns correctly.

    The normal PyRTL user should never need to be aware that this class exists,
    hence the underscore in the name.  It presents a very similar interface to
    wiresVectors (all of the normal wirevector operations should still work),
    but if you try to *set* the value with <<= or |= then it will generate a
    _MemAssignment object rather than the normal wire assignment.
    """

    def __init__(self, mem, index):
        self.mem = mem
        self.index = index
        self.wire = None

    def __ilshift__(self, other):
        return _MemAssignment(rhs=other, is_conditional=False)

    def __ior__(self, other):
        return _MemAssignment(rhs=other, is_conditional=True)

    def _two_var_op(self, other, op):
        return as_wires(self)._two_var_op(other, op)

    def __invert__(self):
        return as_wires(self).__invert__()

    def __getitem__(self, item):
        return as_wires(self).__getitem__(item)

    def __len__(self):
        return self.mem.bitwidth

    def sign_extended(self, bitwidth):
        return as_wires(self).sign_extended(bitwidth)

    def zero_extended(self, bitwidth):
        return as_wires(self).zero_extended(bitwidth)

    @property
    def name(self):
        return as_wires(self).name
        # raise PyrtlError("MemIndexed is a temporary object and therefore doesn't have a name")

    @name.setter
    def name(self, n):
        as_wires(self).name = n


class MemBlock(object):
    """ MemBlock is the object for specifying block memories.  It can be
    indexed like an array for both reading and writing.  Writes under a conditional
    are automatically converted to enabled writes.   For example, consider the following
    examples where `addr`, `data`, and `we` are all WireVectors.

    Usage::

        data = memory[addr]  (infer read port)
        memory[addr] <<= data  (infer write port)
        mem[address] <<= MemBlock.EnabledWrite(data,enable=we)

    When the address of a memory is assigned to using a EnableWrite object
    items will only be written to the memory when the enable WireVector is
    set to high (1).
    """
    # FIXME: write ports assume that only one port is under control of the conditional
    EnabledWrite = collections.namedtuple('EnabledWrite', 'data, enable')
    """ Allows for an enable bit for each write port, where data (the first field in
        the tuple) is the normal data address, and enable (the second field) is a one
        bit signal specifying that the write should happen (i.e. active high)."""

    def __init__(self, bitwidth, addrwidth, name='', max_read_ports=2, max_write_ports=1,
                 asynchronous=False, block=None):
        """ Create a PyRTL read-write memory.

        :param int bitwidth: Defines the bitwidth of each element in the memory
        :param int addrwidth: The number of bits used to address an element of the
         memory. This also defines the size of the memory
        :param str name: The identifier for the memory
        :param max_read_ports: limits the number of read ports each
            block can create; passing `None` indicates there is no limit
        :param max_write_ports: limits the number of write ports each
            block can create; passing `None` indicates there is no limit
        :param bool asynchronous: If false make sure that memory reads are only done
            using values straight from a register. (aka make sure that the
            read is synchronous)
        :param basestring name: Name of the memory. Defaults to an autogenerated
            name
        :param block: The block to add it to, defaults to the working block

        It is best practice to make sure your block memory/fifos read/write
        operations start on a clock edge if you want them to synthesize into efficient hardware.
        MemBlocks will enforce this by making sure that
        you only address them with a register or input, unless you explicitly declare
        the memory as asynchronous with `asynchronous=True` flag.  Note that asynchronous mems
        are, while sometimes very convenient and tempting, rarely a good idea.
        They can't be mapped to block rams in FPGAs and will be converted to registers by most
        design tools even though PyRTL can handle them with no problem.  For any memory beyond
        a few hundred entries it is not a realistic option.

        Each read or write to the memory will create a new `port` (either a read port or write
        port respectively).  By default memories are limited to 2-read and 1-write port, but
        to keep designs efficient by default, but those values can be set as options.  Note
        that memories with high numbers of ports may not be possible to map to physical memories
        such as block rams or existing memory hardware macros.
        """
        self.max_read_ports = max_read_ports
        self.num_read_ports = 0
        self.block = working_block(block)
        name = next_tempvar_name(name)

        if bitwidth <= 0:
            raise PyrtlError('bitwidth must be >= 1')
        if addrwidth <= 0:
            raise PyrtlError('addrwidth must be >= 1')

        self.bitwidth = bitwidth
        self.name = name
        self.addrwidth = addrwidth
        self.readport_nets = []
        self.id = _memIndex.next_index()
        self.asynchronous = asynchronous
        self.block._add_memblock(self)

        self.max_write_ports = max_write_ports
        self.num_write_ports = 0
        self.writeport_nets = []

    def __getitem__(self, item):
        """ Builds circuitry to retrieve an item from the memory """
        item = as_wires(item, bitwidth=self.addrwidth, truncating=False)
        if len(item) > self.addrwidth:
            raise PyrtlError('memory index bitwidth > addrwidth')
        return _MemIndexed(mem=self, index=item)

    def __setitem__(self, item, assignment):
        """ Builds circuitry to set an item in the memory """
        if isinstance(assignment, _MemAssignment):
            self._assignment(item, assignment.rhs, is_conditional=assignment.is_conditional)
        else:
            raise PyrtlError('error, assigment to memories should use "<<=" not "=" operator')

    def _readaccess(self, addr):
        # FIXME: add conditional read ports
        return self._build_read_port(addr)

    def _build_read_port(self, addr):
        if self.max_read_ports is not None:
            self.num_read_ports += 1
            if self.num_read_ports > self.max_read_ports:
                raise PyrtlError('maximum number of read ports (%d) exceeded' % self.max_read_ports)
        data = WireVector(bitwidth=self.bitwidth)
        readport_net = LogicNet(
            op='m',
            op_param=(self.id, self),
            args=(addr,),
            dests=(data,))
        working_block().add_net(readport_net)
        self.readport_nets.append(readport_net)
        return data

    def _assignment(self, item, val, is_conditional):
        from .conditional import _build

        item = as_wires(item, bitwidth=self.addrwidth, truncating=False)
        if len(item) > self.addrwidth:
            raise PyrtlError('error, the wire indexing the memory bitwidth > addrwidth')
        addr = item

        if isinstance(val, MemBlock.EnabledWrite):
            data, enable = val.data, val.enable
        else:
            data, enable = val, Const(1, bitwidth=1)
        data = as_wires(data, bitwidth=self.bitwidth, truncating=False)
        enable = as_wires(enable, bitwidth=1, truncating=False)

        if len(data) != self.bitwidth:
            raise PyrtlError('error, write data larger than memory bitwidth')
        if len(enable) != 1:
            raise PyrtlError('error, enable signal not exactly 1 bit')

        if is_conditional:
            _build(self, (addr, data, enable))
        else:
            self._build(addr, data, enable)

    def _build(self, addr, data, enable):
        """ Builds a write port. """
        if self.max_write_ports is not None:
            self.num_write_ports += 1
            if self.num_write_ports > self.max_write_ports:
                raise PyrtlError('maximum number of write ports (%d) exceeded' %
                                 self.max_write_ports)
        writeport_net = LogicNet(
            op='@',
            op_param=(self.id, self),
            args=(addr, data, enable),
            dests=tuple())
        working_block().add_net(writeport_net)
        self.writeport_nets.append(writeport_net)

    def _make_copy(self, block=None):
        block = working_block(block)
        return MemBlock(bitwidth=self.bitwidth,
                        addrwidth=self.addrwidth,
                        name=self.name,
                        max_read_ports=self.max_read_ports,
                        max_write_ports=self.max_write_ports,
                        asynchronous=self.asynchronous,
                        block=block)


class RomBlock(MemBlock):
    """ PyRTL Read Only Memory.

    RomBlocks are the read only memory block for PyRTL.  They support the same read interface
    and normal memories, but they are cannot be written to (i.e. there are no write ports).
    The ROM must be initialized with some values and construction through the use of the
    `romdata` which is the memory for the system.
    """
    def __init__(self, bitwidth, addrwidth, romdata, name='', max_read_ports=2,
                 build_new_roms=False, asynchronous=False, pad_with_zeros=False, block=None):
        """Create a Python Read Only Memory.

        :param int bitwidth: The bitwidth of each item stored in the ROM
        :param int addrwidth: The bitwidth of the address bus (determines number of addresses)
        :param romdata: This can either be a function or an array (iterable) that maps
            an address as an input to a result as an output
        :param str name: The identifier for the memory
        :param max_read_ports: limits the number of read ports each block can create;
            passing `None` indicates there is no limit
        :param bool build_new_roms: indicates whether to create and pass new RomBlocks during
            `__getitem__` to avoid exceeding `max_read_ports`
        :param bool asynchronous: If false make sure that memory reads are only done
            using values straight from a register. (aka make sure that reads
            are synchronous)
        :param bool pad_with_zeros: If true, extend any missing romdata with zeros out until the
            size of the romblock so that any access to the rom is well defined.  Otherwise, the
            simulation should throw an error on access of unintialized data.  If you are generating
            verilog from the rom, you will need to specify a value for every address (in which case
            setting this to True will help), however for testing and simulation it useful to know if
            you are off the end of explicitly specified values (which is why it is False by default)
        :param block: The block to add to, defaults to the working block
        """

        super(RomBlock, self).__init__(bitwidth=bitwidth, addrwidth=addrwidth, name=name,
                                       max_read_ports=max_read_ports, max_write_ports=0,
                                       asynchronous=asynchronous, block=block)
        self.data = romdata
        self.build_new_roms = build_new_roms
        self.current_copy = self
        self.pad_with_zeros = pad_with_zeros

    def __getitem__(self, item):
        import numbers
        if isinstance(item, numbers.Number):
            raise PyrtlError("There is no point in indexing into a RomBlock with an int. "
                             "Instead, get the value from the source data for this Rom")
            # If you really know what you are doing, use a Const WireVector instead.
        return super(RomBlock, self).__getitem__(item)

    def __setitem__(self, item, assignment):
        raise PyrtlError('no writing to a read-only memory')

    def _get_read_data(self, address):
        import types
        try:
            if address < 0 or address > 2**self.addrwidth - 1:
                raise PyrtlError("Invalid address, " + str(address) + " specified")
        except TypeError:
            raise PyrtlError("Address: {} with invalid type specified".format(address))
        if isinstance(self.data, types.FunctionType):
            try:
                value = self.data(address)
            except Exception:
                raise PyrtlError("Invalid data function for RomBlock")
        else:
            try:
                value = self.data[address]
            except KeyError:
                if self.pad_with_zeros:
                    value = 0
                else:
                    raise PyrtlError(
                        "RomBlock key is invalid, "
                        "consider using pad_with_zeros=True for defaults"
                    )
            except IndexError:
                if self.pad_with_zeros:
                    value = 0
                else:
                    raise PyrtlError(
                        "RomBlock index is invalid, "
                        "consider using pad_with_zeros=True for defaults"
                    )
            except Exception:
                raise PyrtlError("invalid type for RomBlock data object")

        try:
            if value < 0 or value >= 2**self.bitwidth:
                raise PyrtlError("invalid value for RomBlock data")
        except TypeError:
            raise PyrtlError("Value: {} from rom {} has an invalid type"
                             .format(value, self))
        return value

    def _build_read_port(self, addr):
        if self.build_new_roms and \
                (self.current_copy.num_read_ports >= self.current_copy.max_read_ports):
            self.current_copy = self._make_copy()
        return super(RomBlock, self.current_copy)._build_read_port(addr)

    def _make_copy(self, block=None,):
        block = working_block(block)
        return RomBlock(bitwidth=self.bitwidth, addrwidth=self.addrwidth,
                        romdata=self.data, name=self.name, max_read_ports=self.max_read_ports,
                        asynchronous=self.asynchronous, pad_with_zeros=self.pad_with_zeros,
                        block=block)
