""" Helper functions that make constructing hardware easier.
"""

from __future__ import print_function, unicode_literals

import collections
import math
import numbers
import six
import sys
from functools import reduce

from .core import working_block, _NameIndexer, _get_debug_mode
from .pyrtlexceptions import PyrtlError, PyrtlInternalError
from .wire import WireVector, Input, Output, Const, Register
from .corecircuits import as_wires, rtl_all, rtl_any, concat_list

# -----------------------------------------------------------------
#        ___       __   ___  __   __
#  |__| |__  |    |__) |__  |__) /__`
#  |  | |___ |___ |    |___ |  \ .__/
#


probeIndexer = _NameIndexer('Probe-')


def probe(w, name=None):
    """ Print useful information about a WireVector when in debug mode.

    :param w: WireVector from which to get info
    :param name: optional name for probe (defaults to an autogenerated name)
    :return: original WireVector w

    Probe can be inserted into a existing design easily as it returns the
    original wire unmodified. For example ``y <<= x[0:3] + 4`` could be turned
    into ``y <<= probe(x)[0:3] + 4`` to give visibility into both the origin of
    ``x`` (including the line that WireVector was originally created) and
    the run-time values of ``x`` (which will be named and thus show up by
    default in a trace).  Likewise ``y <<= probe(x[0:3]) + 4``,
    ``y <<= probe(x[0:3] + 4)``, and ``probe(y) <<= x[0:3] + 4`` are all
    valid uses of `probe`.

    Note: `probe` does actually add an Output wire to the working block of w (which can
    confuse various post-processing transforms such as output to verilog).
    """
    if not isinstance(w, WireVector):
        raise PyrtlError('Only WireVectors can be probed')

    if name is None:
        name = '(%s: %s)' % (probeIndexer.make_valid_string(), w.name)
    if _get_debug_mode():
        print("Probe: " + name + ' ' + get_stack(w))

    p = Output(name=name)
    p <<= w  # late assigns len from w automatically
    return w


assertIndexer = _NameIndexer('assertion')


def rtl_assert(w, exp, block=None):
    """ Add hardware assertions to be checked on the RTL design.

    :param w: should be a WireVector
    :param Exception exp: Exception to throw when assertion fails
    :param Block block: block to which the assertion should be added (default to working block)
    :return: the Output wire for the assertion (can be ignored in most cases)

    If at any time during execution the wire w is not `true` (i.e. asserted low)
    then simulation will raise exp.
    """
    block = working_block(block)

    if not isinstance(w, WireVector):
        raise PyrtlError('Only WireVectors can be asserted with rtl_assert')
    if len(w) != 1:
        raise PyrtlError('rtl_assert checks only a WireVector of bitwidth 1')
    if not isinstance(exp, Exception):
        raise PyrtlError('the second argument to rtl_assert must be an instance of Exception')
    if isinstance(exp, KeyError):
        raise PyrtlError('the second argument to rtl_assert cannot be a KeyError')
    if w not in block.wirevector_set:
        raise PyrtlError('assertion wire not part of the block to which it is being added')
    if w not in block.wirevector_set:
        raise PyrtlError('assertion not a known wirevector in the target block')

    if w in block.rtl_assert_dict:
        raise PyrtlInternalError('assertion conflicts with existing registered assertion')

    assert_wire = Output(bitwidth=1, name=assertIndexer.make_valid_string(), block=block)
    assert_wire <<= w
    block.rtl_assert_dict[assert_wire] = exp
    return assert_wire


def check_rtl_assertions(sim):
    """ Checks the values in sim to see if any registers assertions fail.

    :param sim: Simulation in which to check the assertions
    :return: None
    """

    for (w, exp) in sim.block.rtl_assert_dict.items():
        try:
            value = sim.inspect(w)
            if not value:
                raise exp
        except KeyError:
            pass


def log2(integer_val):
    """ Return the log base 2 of the integer provided.

    :param integer_val: The integer to take the log base 2 of.
    :return: The log base 2 of integer_val, or throw PyRTL error if not power of 2

    This function is useful when checking that powers of 2 are provided on inputs to functions.
    It throws an error if a negative value is provided or if the value provided is not an even
    power of two.

    Examples: ::

        log2(2)  # returns 1
        log2(256)  # returns 8
        addrwidth = log2(size_of_memory)  # will fail if size_of_memory is not a power of two
    """
    i = integer_val
    if not isinstance(i, int):
        raise PyrtlError('this function can only take integers')
    if i <= 0:
        raise PyrtlError('this function can only take positive numbers 1 or greater')
    if i & (i - 1) != 0:
        raise PyrtlError('this function can only take even powers of 2')
    return i.bit_length() - 1


def truncate(wirevector_or_integer, bitwidth):
    """ Returns a WireVector or integer truncated to the specified bitwidth

    :param wirevector_or_integer: Either a WireVector or an integer to be truncated.
    :param bitwidth: The length to which the first argument should be truncated.
    :return: A truncated WireVector or integer as appropriate.

    This function truncates the most significant bits of the input, leaving a result
    that is only "bitwidth" bits wide.  For integers this is performed with a simple
    bitmask of size "bitwidth".  For WireVectors the function calls 'WireVector.truncate'
    and returns a WireVector of the specified bitwidth.

    Examples: ::

        truncate(9,3)  # returns 1  (0b1001 truncates to 0b001)
        truncate(5,3)  # returns 5  (0b101 truncates to 0b101)
        truncate(-1,3)  # returns 7  (-0b1 truncates to 0b111)
        y = truncate(x+1, x.bitwidth)  # y.bitwdith will equal x.bitwidth
    """
    if bitwidth < 1:
        raise PyrtlError('bitwidth must be a positive integer')
    x = wirevector_or_integer
    try:
        return x.truncate(bitwidth)
    except AttributeError:
        return x & ((1 << bitwidth) - 1)


class MatchedFields(collections.namedtuple('MatchedFields', 'matched fields')):
    def __enter__(self):
        from .conditional import _push_condition
        _push_condition(self.matched)
        return self.fields

    def __exit__(self, *execinfo):
        from .conditional import _pop_condition
        _pop_condition()


def match_bitpattern(w, bitpattern, field_map=None):
    """ Returns a single-bit WireVector that is 1 if and only if 'w' matches the bitpattern,
    and a tuple containining the matched fields, if any. Compatible with the 'with' statement.

    :param w: The WireVector to be compared to the bitpattern
    :param bitpattern: A string holding the pattern (of bits and wildcards) to match
    :param field_map: (optional) A map from single-character field name in the bitpattern
        to the desired name of field in the returned namedtuple. If given, all non-"1"/"0"/"?"
        characters in the bitpattern must be present in the map.
    :return: A tuple of 1-bit WireVector carrying the result of the comparison, followed
        by a named tuple containing the matched fields, if any.

    This function will compare a multi-bit WireVector to a specified pattern of bits, where some
    of the pattern can be "wildcard" bits.  If any of the "1" or "0" values specified in the
    bitpattern fail to match the WireVector during execution, a "0" will be produced, otherwise
    the value carried on the wire will be "1".  The wildcard characters can be any other
    alphanumeric character, with characters other than "?" having special functionality (see below).
    The string must have length equal to the wirevector specified, although whitespace and
    underscore characters will be ignored and can be used for pattern readability.

    For all other characters besides "1", "0", or "?", a tuple of WireVectors will be returned as
    the second return value. Each character will be treated as the name of a field,
    and non-consecutive fields with the same name will be concatenated together, left-to-right, into
    a single field in the resultant tuple. For example, "01aa1?bbb11a" will match a string such
    as "010010100111", and the resultant matched fields are (a, b) = (0b001, 0b100), where the 'a'
    field is the concenation of bits 9, 8, and 0, and the 'b' field is the concenation of
    bits 5, 4, and 3. Thus, arbitrary characters beside "?" act as wildcard characters for the
    purposes of matching, with the additional benefit of returning the wirevectors corresponding
    to those fields.

    A prime example of this is for decoding an ISA's instructions. Here we decode some RISC-V: ::

        with pyrtl.conditional_assignment:
            with match_bitpattern(inst, "iiiiiiiiiiiirrrrr010ddddd0000011") as (imm, rs1, rd):
                regfile[rd] |= mem[(regfile[rs1] + imm.sign_extended(32)).truncate(32)]
                pc.next |= pc + 1
            with match_bitpattern(inst, "iiiiiiirrrrrsssss010iiiii0100011") as (imm, rs2, rs1):
                mem[(regfile[rs1] + imm.sign_extended(32)).truncate(32)] |= regfile[rs2]
                pc.next |= pc + 1
            with match_bitpattern(inst, "0000000rrrrrsssss111ddddd0110011") as (rs2, rs1, rd):
                regfile[rd] |= regfile[rs1] & regfile[rs2]
                pc.next |= pc + 1
            with match_bitpattern(inst, "0000000rrrrrsssss000ddddd0110011") as (rs2, rs1, rd):
                regfile[rd] |= (regfile[rs1] + regfile[rs2]).truncate(32)
                pc.next |= pc + 1
            # ...etc...

    Some smaller examples: ::

        m, _ = match_bitpattern(w, '0101')  # basically the same as w=='0b0101'
        m, _ = match_bitpattern(w, '01?1')  # m will be true when w is '0101' or '0111'
        m, _ = match_bitpattern(w, '??01')  # m be true when last two bits of w are '01'
        m, _ = match_bitpattern(w, '??_0 1')  # spaces/underscores are ignored, same as line above
        m, (a, b) = match_pattern(w, '01aa1?bbb11a')  # all bits with same letter make up same field
        m, fs = match_pattern(w, '01aa1?bbb11a', {'a': 'foo', 'b': 'bar'})  # fields fs.foo, fs.bar
    """
    w = as_wires(w)
    if not isinstance(bitpattern, six.string_types):
        raise PyrtlError('bitpattern must be a string')
    nospace_string = ''.join(bitpattern.replace('_', '').split())
    if len(w) != len(nospace_string):
        raise PyrtlError('bitpattern string different length than wirevector provided')
    lsb_first_string = nospace_string[::-1]  # flip so index 0 is lsb

    zero_bits = [w[index] for index, x in enumerate(lsb_first_string) if x == '0']
    one_bits = [w[index] for index, x in enumerate(lsb_first_string) if x == '1']
    match = rtl_all(*one_bits) & ~rtl_any(*zero_bits)

    # Since only Python 3.7 and above guarantees maintaining insertion order in dictionaries,
    # do all of this to make sure we can maintain the ordering in the returned Tuple.
    # Order of fields is determined based on left-to-right ordering in original string.
    def field_name(name):
        if field_map is not None:
            if name not in field_map:
                raise PyrtlError('field_map argument has been given, '
                                 'but %s field is not present' % name)
            return field_map[name]
        return name

    fields = collections.defaultdict(list)
    for i, c in enumerate(lsb_first_string):
        if c not in '01?':
            fields[c].append(w[i])
    fields = sorted(fields.items(), key=lambda m: nospace_string.index(m[0]))  # now list of tuples
    Fields = collections.namedtuple('Fields', ' '.join(field_name(name) for name, _ in fields))
    fields = Fields(**{field_name(k): concat_list(l) for k, l in fields})

    return MatchedFields(match, fields)


def bitpattern_to_val(bitpattern, *ordered_fields, **named_fields):
    """ Return an unsigned integer representation of field format filled with the provided values.

    :param bitpattern: A string holding the pattern (of bits and wildcards) to match
    :param ordered_fields: A list of parameters to be matched to the provided bit pattern in
        the order provided.  If ordered_fields are provided then no named_fields can be used.
    :param named_fields: A list of parameters to be matched to the provided bit pattern in
        by the names provided.  If named_fields are provided then no ordered_fields can be used.
        A special keyword argument, 'field_map', can be provided, which will allow you specify
        a correspondence between the 1-letter field names in the bitpattern string and longer,
        human readable field names (see example below).
    :return: An unsigned integer carrying the result of the field substitution.

    This function will compare take a specified pattern of bits, where some
    of the pattern can be "wildcard" bits.  The wildcard bits must all be named with a single
    letter and, unlike the related function ``match_bitpattern``, no "?" can be used.  The function
    will take the provided bitpattern and create an integer that substitutes the provided fields
    in for the given wildcards at the bit level.  This sort of bit substitution is useful when
    creating values for testing when the resulting values will be "chopped" up by the hardware
    later (e.g. instruction decode or other bitfield heavy functions).

    If a special keyword argument, 'field_map', is provided, then the named fields provided
    can be longer, human-readable field names, which will correspond to the field in the
    bitpattern according to the field_map. See the third example below.

    Examples::

        bitpattern_to_val('0000000sssssrrrrr000ddddd0110011', s=2, r=1, d=3)  # RISCV ADD instr
        # evaluates to   0b00000000000100010000000110110011

        bitpattern_to_val('iiiiiiisssssrrrrr010iiiii0100011', i=1, s=4, r=3)  # RISCV SW instr
        # evaluates to   0b00000000001100100010000010100011

        bitpattern_to_val(
            'iiiiiiisssssrrrrr010iiiii0100011',
            imm=1, rs2=4, rs1=3,
            field_map={'i': 'imm', 's': 'rs2', 'r': 'rs1}
        )  # RISCV SW instr
        # evaluates to   0b00000000001100100010000010100011
    """

    if len(ordered_fields) > 0 and len(named_fields) > 0:
        raise PyrtlError('named and ordered fields cannot be mixed')

    def letters_in_field_order():
        seen = []
        for c in bitpattern:
            if c != '0' and c != '1' and c not in seen:
                seen.append(c)
        return seen

    field_map = None
    if 'field_map' in named_fields:
        field_map = named_fields['field_map']
        named_fields.pop('field_map')

    bitlist = []
    lifo = letters_in_field_order()
    if ordered_fields:
        if len(lifo) != len(ordered_fields):
            raise PyrtlError('number of fields and number of unique patterns do not match')
        intfields = [int(f) for f in ordered_fields]
    else:
        if len(lifo) != len(named_fields):
            raise PyrtlError('number of fields and number of unique patterns do not match')
        try:
            def fn(n):
                return field_map[n] if field_map else n
            intfields = [int(named_fields[fn(n)]) for n in lifo]
        except KeyError as e:
            raise PyrtlError('bitpattern field %s was not provided in named_field list' % e.args[0])

    fmap = dict(zip(lifo, intfields))
    for c in bitpattern[::-1]:
        if c == '0' or c == '1':
            bitlist.append(c)
        elif c == '?':
            raise PyrtlError('all fields in the bitpattern must have names')
        else:
            bitlist.append(str(fmap[c] & 0x1))  # append lsb of the field
            fmap[c] = fmap[c] >> 1  # and bit shift by one position
    for f in fmap:
        if fmap[f] not in [0, -1]:
            raise PyrtlError('too many bits given to value to fit in field %s' % f)
    if len(bitpattern) != len(bitlist):
        raise PyrtlInternalError('resulting values have different bitwidths')
    final_str = ''.join(bitlist[::-1])
    return int(final_str, 2)


def chop(w, *segment_widths):
    """ Returns a list of WireVectors each a slice of the original 'w'

    :param w: The WireVector to be chopped up into segments
    :param segment_widths: Additional arguments are integers which are bitwidths
    :return: A list of WireVectors each with a proper segment width

    This function chops a WireVector into a set of smaller WireVectors of different
    lengths.  It is most useful when multiple "fields" are contained with a single
    wirevector, for example when breaking apart an instruction.  For example, if
    you wish to break apart a 32-bit MIPS I-type (Immediate) instruction you know
    it has an 6-bit opcode, 2 5-bit operands, and 16-bit offset.  You could take
    each of those slices in absolute terms: offset=instr[0:16], rt=instr[16:21]
    and so on, but then you have to do the arithmetic yourself.  With this function
    you can do all the fields at once which can be seen in the examples below.

    As a check, chop will throw an error if the sum of the lengths of the fields
    given is not the same as the length of the wirevector to chop.  Not also that
    chop assumes that the "rightmost" arguments are the least signficant bits
    (just like pyrtl concat) which is normal for hardware functions but makes the
    list order a little counter intuitive.

    Examples: ::

        opcode, rs, rt, offset = chop(instr, 6, 5, 5, 16)  # MIPS I-type instruction
        opcode, instr_index = chop(instr, 6, 26)  # MIPS J-type instruction
        opcode, rs, rt, rd, sa, function = chop(instr, 6, 5, 5, 5, 5, 6)  # MIPS R-type
        msb, middle, lsb = chop(data, 1, 30, 1) # breaking out the most and least sig bit
    """
    w = as_wires(w)
    for seg in segment_widths:
        if not isinstance(seg, int):
            raise PyrtlError('segment widths must be integers')
    if sum(segment_widths) != len(w):
        raise PyrtlError('sum of segment widths must equal length of wirevetor')

    n_segments = len(segment_widths)
    starts = [sum(segment_widths[i + 1:]) for i in range(n_segments)]
    ends = [sum(segment_widths[i:]) for i in range(n_segments)]
    return [w[s:e] for s, e in zip(starts, ends)]


def input_list(names, bitwidth=None):
    """ Allocate and return a list of Inputs.

    :param names: Names for the Inputs. Can be a list or single comma/space-separated string
    :param bitwidth: The desired bitwidth for the resulting Inputs.
    :return: List of Inputs.

    Equivalent to: ::

        wirevector_list(names, bitwidth, wvtype=pyrtl.wire.Input)

    """
    return wirevector_list(names, bitwidth, wvtype=Input)


def output_list(names, bitwidth=None):
    """ Allocate and return a list of Outputs.

    :param names: Names for the Outputs. Can be a list or single comma/space-separated string
    :param bitwidth: The desired bitwidth for the resulting Outputs.
    :return: List of Outputs.

    Equivalent to: ::

        wirevector_list(names, bitwidth, wvtype=pyrtl.wire.Output)

    """
    return wirevector_list(names, bitwidth, wvtype=Output)


def register_list(names, bitwidth=None):
    """ Allocate and return a list of Registers.

    :param names: Names for the Registers. Can be a list or single comma/space-separated string
    :param bitwidth: The desired bitwidth for the resulting Registers.
    :return: List of Registers.

    Equivalent to: ::

        wirevector_list(names, bitwidth, wvtype=pyrtl.wire.Register)

    """
    return wirevector_list(names, bitwidth, wvtype=Register)


def wirevector_list(names, bitwidth=None, wvtype=WireVector):
    """ Allocate and return a list of WireVectors.

    :param names: Names for the WireVectors. Can be a list or single comma/space-separated string
    :param bitwidth: The desired bitwidth for the resulting WireVectors.
    :param WireVector wvtype: Which WireVector type to create.
    :return: List of WireVectors.

    Additionally, the ``names`` string can also contain an additional bitwidth specification
    separated by a ``/`` in the name. This cannot be used in combination with a ``bitwidth``
    value other than ``1``.

    Examples: ::

        wirevector_list(['name1', 'name2', 'name3'])
        wirevector_list('name1, name2, name3')
        wirevector_list('input1 input2 input3', bitwidth=8, wvtype=pyrtl.wire.Input)
        wirevector_list('output1, output2 output3', bitwidth=3, wvtype=pyrtl.wire.Output)
        wirevector_list('two_bits/2, four_bits/4, eight_bits/8')
        wirevector_list(['name1', 'name2', 'name3'], bitwidth=[2, 4, 8])

    """
    if isinstance(names, str):
        names = names.replace(',', ' ').split()

    if any('/' in name for name in names) and bitwidth is not None:
        raise PyrtlError('only one of optional "/" or bitwidth parameter allowed')

    if bitwidth is None:
        bitwidth = 1
    if isinstance(bitwidth, numbers.Integral):
        bitwidth = [bitwidth] * len(names)
    if len(bitwidth) != len(names):
        raise ValueError('number of names ' + str(len(names))
                         + ' should match number of bitwidths ' + str(len(bitwidth)))

    wirelist = []
    for fullname, bw in zip(names, bitwidth):
        try:
            name, bw = fullname.split('/')
        except ValueError:
            name, bw = fullname, bw
        wirelist.append(wvtype(bitwidth=int(bw), name=name))
    return wirelist


def val_to_signed_integer(value, bitwidth):
    """ Return value as intrepreted as a signed integer under twos complement.

    :param value: a python integer holding the value to convert
    :param bitwidth: the length of the integer in bits to assume for conversion

    Given an unsigned integer (not a wirevector!) covert that to a signed
    integer.  This is useful for printing and interpreting values which are
    negative numbers in twos complement. ::

        val_to_signed_integer(0xff, 8) == -1
    """
    if isinstance(value, WireVector) or isinstance(bitwidth, WireVector):
        raise PyrtlError('inputs must not be wirevectors')
    if bitwidth < 1:
        raise PyrtlError('bitwidth must be a positive integer')

    neg_mask = 1 << (bitwidth - 1)
    neg_part = value & neg_mask

    pos_mask = neg_mask - 1
    pos_part = value & pos_mask

    return pos_part - neg_part


def formatted_str_to_val(data, format, enum_set=None):
    """ Return an unsigned integer representation of the data given format specified.

    :param data: a string holding the value to convert
    :param format: a string holding a format which will be used to convert the data string
    :param enum_set: an iterable of enums which are used as part of the conversion process

    Given a string (not a wirevector!) covert that to an unsigned integer ready for input
    to the simulation enviornment.  This helps deal with signed/unsigned numbers (simulation
    assumes the values have been converted via two's complement already), but it also takes
    hex, binary, and enum types as inputs.  It is easiest to see how it works with some
    examples. ::

        formatted_str_to_val('2', 's3') == 2  # 0b010
        formatted_str_to_val('-1', 's3') == 7  # 0b111
        formatted_str_to_val('101', 'b3') == 5
        formatted_str_to_val('5', 'u3') == 5
        formatted_str_to_val('-3', 's3') == 5
        formatted_str_to_val('a', 'x3') == 10
        class Ctl(Enum):
            ADD = 5
            SUB = 12
        formatted_str_to_val('ADD', 'e3/Ctl', [Ctl]) == 5
        formatted_str_to_val('SUB', 'e3/Ctl', [Ctl]) == 12

    """
    type = format[0]
    bitwidth = int(format[1:].split('/')[0])
    bitmask = (1 << bitwidth) - 1
    if type == 's':
        rval = int(data) & bitmask
    elif type == 'x':
        rval = int(data, 16)
    elif type == 'b':
        rval = int(data, 2)
    elif type == 'u':
        rval = int(data)
        if rval < 0:
            raise PyrtlError('unsigned format requested, but negative value provided')
    elif type == 'e':
        enumname = format.split('/')[1]
        enum_inst_list = [e for e in enum_set if e.__name__ == enumname]
        if len(enum_inst_list) == 0:
            raise PyrtlError('enum "{}" not found in passed enum_set "{}"'
                             .format(enumname, enum_set))
        rval = getattr(enum_inst_list[0], data).value
    else:
        raise PyrtlError('unknown format type {}'.format(format))
    return rval


def val_to_formatted_str(val, format, enum_set=None):
    """ Return a string representation of the value given format specified.

    :param val: an unsigned integer to convert
    :param format: a string holding a format which will be used to convert the data string
    :param enum_set: an iterable of enums which are used as part of the converstion process

    Given an unsigned integer (not a wirevector!) covert that to a string ready for output
    to a human to interpret.  This helps deal with signed/unsigned numbers (simulation
    operates on values that have been converted via two's complement), but it also generates
    hex, binary, and enum types as outputs.  It is easiest to see how it works with some
    examples. ::

        val_to_formatted_str(2, 's3') == '2'
        val_to_formatted_str(7, 's3') == '-1'
        val_to_formatted_str(5, 'b3') == '101'
        val_to_formatted_str(5, 'u3') == '5'
        val_to_formatted_str(5, 's3') == '-3'
        val_to_formatted_str(10, 'x3') == 'a'
        class Ctl(Enum):
            ADD = 5
            SUB = 12
        val_to_formatted_str(5, 'e3/Ctl', [Ctl]) == 'ADD'
        val_to_formatted_str(12, 'e3/Ctl', [Ctl]) == 'SUB'

    """
    type = format[0]
    bitwidth = int(format[1:].split('/')[0])
    bitmask = (1 << bitwidth) - 1
    if type == 's':
        rval = str(val_to_signed_integer(val, bitwidth))
    elif type == 'x':
        rval = hex(val)[2:]  # cuts off '0x' at the start
    elif type == 'b':
        rval = bin(val)[2:]  # cuts off '0b' at the start
    elif type == 'u':
        rval = str(int(val))  # nothing fancy
    elif type == 'e':
        enumname = format.split('/')[1]
        enum_inst_list = [e for e in enum_set if e.__name__ == enumname]
        if len(enum_inst_list) == 0:
            raise PyrtlError('enum "{}" not found in passed enum_set "{}"'
                             .format(enumname, enum_set))
        rval = enum_inst_list[0](val).name
    else:
        raise PyrtlError('unknown format type {}'.format(format))
    return rval


# this is the return type of value_bitwidth_tuple
ValueBitwidthTuple = collections.namedtuple('ValueBitwidthTuple', 'value bitwidth')


def infer_val_and_bitwidth(rawinput, bitwidth=None, signed=False):
    """ Return a tuple (value, bitwidth) infered from the specified input.

    :param rawinput: a bool, int, or verilog-style string constant
    :param bitwidth: an integer bitwidth or (by default) None
    :param signed: a bool (by default set False) to include bits for proper twos complement
    :return: tuple of integers (value, bitwidth)

    Given a boolean, integer, or verilog-style string constant, this function returns a
    tuple of two integers (value, bitwidth) which are infered from the specified rawinput.
    The tuple returned is, in fact, a named tuple with names .value and .bitwidth for feilds
    0 and 1 respectively.  If signed is set to true, bits will be included to ensure a proper
    two's complement representation is possible, otherwise it is assume all bits can be used
    for standard unsigned representation.  Error checks are performed that determine if the
    bitwidths specified are sufficient and appropriate for the values specified.
    Examples can be found below ::

        infer_val_and_bitwidth(2, bitwidth=5) == (2, 5)
        infer_val_and_bitwidth(3) == (3, 2)  # bitwidth infered from value
        infer_val_and_bitwidth(3, signed=True) == (3, 3)  # need a bit for the leading zero
        infer_val_and_bitwidth(-3, signed=True) == (5, 3)  # 5 = -3 & 0b111 = ..111101 & 0b111
        infer_val_and_bitwidth(-4, signed=True) == (4, 3)  # 4 = -4 & 0b111 = ..111100 & 0b111
        infer_val_and_bitwidth(-3, bitwidth=5, signed=True) == (29, 5)
        infer_val_and_bitwidth(-3) ==> Error  # negative numbers require bitwidth or signed=True
        infer_val_and_bitwidth(3, bitwidth=2) == (3, 2)
        infer_val_and_bitwidth(3, bitwidth=2, signed=True) ==> Error  # need space for sign bit
        infer_val_and_bitwidth(True) == (1, 1)
        infer_val_and_bitwidth(False) == (0, 1)
        infer_val_and_bitwidth("5'd12") == (12, 5)
        infer_val_and_bitwidth("5'b10") == (2, 5)
        infer_val_and_bitwidth("5'b10").bitwidth == 5
        infer_val_and_bitwidth("5'b10").value == 2
        infer_val_and_bitwidth("8'B 0110_1100") == (108, 8)
    """

    if isinstance(rawinput, bool):
        return _convert_bool(rawinput, bitwidth, signed)
    elif isinstance(rawinput, numbers.Integral):
        return _convert_int(rawinput, bitwidth, signed)
    elif isinstance(rawinput, six.string_types):
        return _convert_verilog_str(rawinput, bitwidth, signed)
    else:
        raise PyrtlError('error, the value provided is of an improper type, "%s"'
                         'proper types are bool, int, and string' % type(rawinput))


def _convert_bool(bool_val, bitwidth=None, signed=False):
    if signed:
        raise PyrtlError('error, booleans cannot be signed (covert to int first)')
    num = int(bool_val)
    if bitwidth is None:
        bitwidth = 1
    if bitwidth != 1:
        raise PyrtlError('error, boolean has bitwidth not equal to 1')
    return ValueBitwidthTuple(num, bitwidth)


def _convert_int(val, bitwidth=None, signed=False):
    if val >= 0:
        num = val
        # infer bitwidth if it is not specified explicitly
        min_bitwidth = len(bin(num)) - 2  # the -2 for the "0b" at the start of the string
        if signed and val != 0:
            min_bitwidth += 1  # extra bit needed for the zero

        if bitwidth is None:
            bitwidth = min_bitwidth
        elif bitwidth < min_bitwidth:
            raise PyrtlError('bitwidth specified is insufficient to represent constant')

    else:  # val is negative
        if not signed and bitwidth is None:
            raise PyrtlError('negative constants require either signed=True or specified bitwidth')

        if bitwidth is None:
            bitwidth = 1 if val == -1 else len(bin(~val)) - 1

        if (val >> bitwidth - 1) != -1:
            raise PyrtlError('insufficient bits for negative number')

        num = val & ((1 << bitwidth) - 1)  # result is a twos complement value
    return ValueBitwidthTuple(num, bitwidth)


def _convert_verilog_str(val, bitwidth=None, signed=False):
    if signed:
        raise PyrtlError('error, "signed" option with verilog-style string constants not supported')

    bases = {'b': 2, 'o': 8, 'd': 10, 'h': 16, 'x': 16}
    passed_bitwidth = bitwidth

    neg = False
    if val.startswith('-'):
        neg = True
        val = val[1:]
    split_string = val.lower().split("'")
    if len(split_string) != 2:
        raise PyrtlError('error, string not in verilog style format')
    try:
        bitwidth = int(split_string[0])
        sval = split_string[1]
        if sval[0] == 's':
            raise PyrtlError('error, signed integers are not supported in verilog-style constants')
        base = 10
        if sval[0] in bases:
            base = bases[sval[0]]
            sval = sval[1:]
        sval = sval.replace('_', '')
        num = int(sval, base)
    except (IndexError, ValueError):
        raise PyrtlError('error, string not in verilog style format')
    if neg and num:
        if (num >> bitwidth - 1):
            raise PyrtlError('error, insufficient bits for negative number')
        num = (1 << bitwidth) - num

    if passed_bitwidth and passed_bitwidth != bitwidth:
        raise PyrtlError('error, bitwidth parameter of constant does not match'
                         ' the bitwidth infered from the verilog style specification'
                         ' (if bitwidth=None is used, pyrtl will determine the bitwidth from the'
                         ' verilog-style constant specification)')

    if num >> bitwidth != 0:
        raise PyrtlError('specified bitwidth %d for verilog constant insufficient to store value %d'
                         % (bitwidth, num))

    return ValueBitwidthTuple(num, bitwidth)


def get_stacks(*wires):
    call_stack = getattr(wires[0], 'init_call_stack', None)
    if not call_stack:
        return '    No call info found for wires: use set_debug_mode() ' \
               'to provide more information\n'
    else:
        return '\n'.join(str(wire) + ":\n" + get_stack(wire) for wire in wires)


def get_stack(wire):
    if not isinstance(wire, WireVector):
        raise PyrtlError('Only WireVectors can be traced')

    call_stack = getattr(wire, 'init_call_stack', None)
    if call_stack:
        frames = ' '.join(frame for frame in call_stack[:-1])
        return "Wire Traceback, most recent call last \n" + frames + "\n"
    else:
        return '    No call info found for wire: use set_debug_mode()'\
               ' to provide more information'


def _check_for_loop(block=None):
    block = working_block(block)
    logic_left = block.logic.copy()
    wires_left = block.wirevector_subset(exclude=(Input, Const, Output, Register))
    prev_logic_left = len(logic_left) + 1
    while prev_logic_left > len(logic_left):
        prev_logic_left = len(logic_left)
        nets_to_remove = set()  # bc it's not safe to mutate a set inside its own iterator
        for net in logic_left:
            if not any(n_wire in wires_left for n_wire in net.args):
                nets_to_remove.add(net)
                wires_left.difference_update(net.dests)
        logic_left -= nets_to_remove

    if 0 == len(logic_left):
        return None
    return wires_left, logic_left


def find_loop(block=None):
    block = working_block(block)
    block.sanity_check()  # make sure that the block is sane first

    result = _check_for_loop(block)
    if not result:
        return
    wires_left, logic_left = result
    import random

    class _FilteringState(object):
        def __init__(self, dst_w):
            self.dst_w = dst_w
            self.arg_num = -1

    def dead_end():
        # clean up after a wire is found to not be part of the loop
        wires_left.discard(cur_item.dst_w)
        current_wires.discard(cur_item.dst_w)
        del checking_stack[-1]

    # now making a map to quickly look up nets
    dest_nets = {dest_w: net_ for net_ in logic_left for dest_w in net_.dests}
    initial_w = random.sample(wires_left, 1)[0]

    current_wires = set()
    checking_stack = [_FilteringState(initial_w)]

    # we don't use a recursive method as Python has a limited stack (default: 999 frames)
    while len(checking_stack):
        cur_item = checking_stack[-1]
        if cur_item.arg_num == -1:
            #  first time testing this item
            if cur_item.dst_w not in wires_left:
                dead_end()
                continue
            current_wires.add(cur_item.dst_w)
            cur_item.net = dest_nets[cur_item.dst_w]
            if cur_item.net.op == 'r':
                dead_end()
                continue
        cur_item.arg_num += 1  # go to the next item
        if cur_item.arg_num == len(cur_item.net.args):
            dead_end()
            continue
        next_wire = cur_item.net.args[cur_item.arg_num]
        if next_wire not in current_wires:
            current_wires.add(next_wire)
            checking_stack.append(_FilteringState(next_wire))
        else:  # We have found the loop!!!!!
            loop_info = []
            for f_state in reversed(checking_stack):
                loop_info.append(f_state)
                if f_state.dst_w is next_wire:
                    break
            else:
                raise PyrtlError("Shouldn't get here! Couldn't figure out the loop")
            return loop_info
    raise PyrtlError("Error in detecting loop")


def find_and_print_loop(block=None):
    loop_data = find_loop(block)
    print_loop(loop_data)
    return loop_data


def print_loop(loop_data):
    if not loop_data:
        print("No Loop Found")
    else:
        print("Loop found:")
        print('\n'.join("{}".format(fs.net) for fs in loop_data))
        # print '\n'.join("{} (dest wire: {})".format(fs.net, fs.dst_w) for fs in loop_info)
        print("")


def _currently_in_jupyter_notebook():
    """ Return true if running under Jupyter notebook, otherwise return False.

    We want to check for more than just the presence of __IPYTHON__ because
    that is present in both Jupyter notebooks and IPython terminals.
    """
    try:
        # get_ipython() is in the global namespace when ipython is started
        shell = get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return False  # Other type
    except NameError:
        return False      # Probably standard Python interpreter


def _print_netlist_latex(netlist):
    """ Print each net in netlist in a Latex array """
    from IPython.display import display, Latex  # pylint: disable=import-error
    out = '\n\\begin{array}{ \\| c \\| c \\| l \\| }\n'
    out += '\n\\hline\n'
    out += '\\hline\n'.join(str(n) for n in netlist)
    out += '\\hline\n\\end{array}\n'
    display(Latex(out))


class _NetCount(object):
    """ Helper class to track when to stop an iteration that depends on number of nets

    Mainly useful for iterations that are for optimization
    """
    def __init__(self, block=None):
        self.block = working_block(block)
        self.prev_nets = len(self.block.logic) * 1000

    def shrank(self, block=None, percent_diff=0, abs_diff=1):
        """
        Returns whether a block has fewer nets than before

        :param Block block: block to check (if changed)
        :param Number percent_diff: percentage difference threshold
        :param int abs_diff: absolute difference threshold
        :return: boolean

        This function checks whether the change in the number of
        nets is greater than the percentage and absolute difference
        thresholds.
        """
        if block is None:
            block = self.block
        cur_nets = len(block.logic)
        net_goal = self.prev_nets * (1 - percent_diff) - abs_diff
        less_nets = (cur_nets <= net_goal)
        self.prev_nets = cur_nets
        return less_nets

    shrinking = shrank
