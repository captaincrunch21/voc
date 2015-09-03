import dis

from ..java import (
    Code as JavaCode,
    opcodes as JavaOpcodes,
    ExceptionInfo as JavaExceptionInfo,
    LineNumberTable
)

from .utils import extract_command
from .opcodes import ASTORE_name, ALOAD_name, IF, END_IF, DEBUG


class IgnoreBlock(Exception):
    """An escape hatch; enable a block to be flagged as ignorable"""
    pass


class Block:
    def __init__(self, parent=None, commands=None):
        self.parent = parent
        self.commands = commands if commands else []
        self.localvars = {}

        self.code = []
        self.try_catches = []
        self.if_blocks = []
        self.source_lines = []
        self.next_resolve_list = []
        self.next_opcode_starts_line = None

    def store_name(self, name, arguments, allow_locals=True):
        if allow_locals:
            return [
                ASTORE_name(self.localvars, name)
            ]
        else:
            return [
                ASTORE_name(self.localvars, '#TEMP#'),
                JavaOpcodes.GETSTATIC(self.klass.descriptor, 'attrs', 'Ljava/util/Hashtable;'),
                JavaOpcodes.LDC(name),
                ALOAD_name(self.localvars, '#TEMP#'),
                JavaOpcodes.INVOKEVIRTUAL('java/util/Hashtable', 'put', '(Ljava/lang/Object;Ljava/lang/Object;)Ljava/lang/Object;'),
                JavaOpcodes.POP(),
            ]

    def load_name(self, name, allow_locals=True):
        code = []
        try:
            # Look for a local first.
            if allow_locals:
                code.append(ALOAD_name(self.localvars, name))
            else:
                code.append(DEBUG("Ignoring locals in search for %s" % name))
                raise KeyError('Not scanning locals')
        except KeyError:
            code.extend([
                # If there isn't a local, look for a global
                JavaOpcodes.GETSTATIC(self.module.descriptor, 'globals', 'Ljava/util/Hashtable;'),
                JavaOpcodes.LDC(name),
                JavaOpcodes.INVOKEVIRTUAL('java/util/Hashtable', 'get', '(Ljava/lang/Object;)Ljava/lang/Object;'),

                # If there's nothing in the globals, then look for a builtin.
                IF(
                    [JavaOpcodes.DUP()],
                    JavaOpcodes.IFNONNULL
                ),
                    DEBUG('%s not found in globals' % name),
                    JavaOpcodes.POP(),
                    JavaOpcodes.GETSTATIC('org/Python', 'builtins', 'Ljava/util/Hashtable;'),
                    JavaOpcodes.LDC(name),
                    JavaOpcodes.INVOKEVIRTUAL('java/util/Hashtable', 'get', '(Ljava/lang/Object;)Ljava/lang/Object;'),
                END_IF()
            ])

        return code

    def extract(self, code):
        """Break a code object into the parts it defines, populating the
        provided block.

        """
        instructions = list(dis.Bytecode(code))
        i = len(instructions)
        commands = []
        while i > 0:
            i, command = extract_command(instructions, i)
            commands.append(command)

        commands.reverse()

        print ('=====' * 10)
        print (code)
        print ('-----' * 10)
        for command in commands:
            command.dump()
        print ('=====' * 10)

        # Append the extracted commands to any pre-existing ones.
        self.commands.extend(commands)

    def tweak(self):
        """Tweak the bytecode generated for this block."""
        pass

    def add_opcodes(self, *opcodes):
        # If we've flagged a code line change, attach that to the opcode
        if self.next_opcode_starts_line:
            opcodes[0].starts_line = self.next_opcode_starts_line
            # print("RECORD CODE START", opcodes[0])
            self.next_opcode_starts_line = None

        # Add the opcodes to the code list.
        self.code.extend(opcodes)

        # Resolve any references to the "next" opcode.
        for (obj, attr) in self.next_resolve_list:
            # print("        resolve %s reference on %s with %s" % (attr, id(obj), opcodes[0]))
            setattr(obj, attr, opcodes[0])
            opcodes[0].references.append((obj, attr))
        self.next_resolve_list = []

    def stack_depth(self):
        "Evaluate the maximum stack depth required by a sequence of Java opcodes"
        depth = 0
        max_depth = 0
        for opcode in self.code:
            # print("   ", opcode)
            depth = depth + opcode.stack_effect
            if depth > max_depth:
                max_depth = depth
        return max_depth

    def ignore_empty(self):
        if len(self.code) == 1 and isinstance(self.code[0], JavaOpcodes.RETURN):
            raise IgnoreBlock()
        elif len(self.code) == 2 and isinstance(self.code[1], JavaOpcodes.ARETURN):
            raise IgnoreBlock()

    def void_return(self):
        """Ensure that end of the code sequence is a Java-style return of void.

        Java has a separate opcode for VOID returns, which is different to
        RETURN NULL. Replace "SET NULL" "ARETURN" pair with "RETURN".
        """

        if len(self.code) >= 2 and isinstance(self.code[-2], JavaOpcodes.ACONST_NULL) and isinstance(self.code[-1], JavaOpcodes.ARETURN):
            # There might be opcodes referencing these two - in particular if
            # the last thing in the function is the end of an IF or TRY block.
            # Find all the blocks that reference these two opcodes and update
            # the references.
            return_opcode = JavaOpcodes.RETURN()
            for obj, attr in self.code[-2].references:
                setattr(obj, attr, return_opcode)
            for obj, attr in self.code[-1].references:
                setattr(obj, attr, return_opcode)

            # Then, check to see if either opcode had a line number association.
            # if so, preserve the first one.
            if self.code[-2].starts_line is not None:
                return_opcode.starts_line = self.code[-2].starts_line
            elif self.code[-1].starts_line is not None:
                return_opcode.starts_line = self.code[-1].starts_line

            self.code = self.code[:-2] + [return_opcode]

    def transpile(self):
        """Create a JavaCode object representing the commands stored in the block

        May raise ``IgnoreBlock`` if the block should be ignored.
        """
        # Convert the sequence of commands into instructions.
        # Most of the instructions will be opcodes. However, some will
        # be instructions to add exception blocks, line number references, etc
        for cmd in self.commands:
            for instruction in cmd.operation.transpile(self, cmd.arguments):
                if instruction.starts_line:
                    self.next_opcode_starts_line = instruction.starts_line
                instruction.process(self)

        # Java requires that every body of code finishes with a return.
        # Make sure there is one.
        if len(self.code) == 0 or not isinstance(self.code[-1], (JavaOpcodes.RETURN, JavaOpcodes.ARETURN)):
            self.add_opcodes(JavaOpcodes.RETURN())

        # Provide any tweaks that are needed because of the context in which
        # the block is being used.
        self.tweak()

        # Now that we have a complete opcode list, postprocess the list
        # with the known offsets.
        offset = 0
        for index, instruction in enumerate(self.code):
            instruction.code_index = index
            instruction.code_offset = offset
            offset += len(instruction)

        # Construct the exception table, updating any
        # end-of-exception GOTO operations with the right opcode.
        # Record a frame range for each one.
        exceptions = []
        for try_catch in self.try_catches:
            # print("TRY CATCH START", id(try_catch), try_catch.start_op, try_catch.start_op.code_offset)
            # print("            END", try_catch.end_op)
            for handler in try_catch.handlers:
                exceptions.append(JavaExceptionInfo(
                    try_catch.start_op.code_offset,
                    try_catch.jump_op.code_offset,
                    handler.start_op.code_offset,
                    handler.descriptor
                ))

            try_catch.jump_op.offset = try_catch.end_op.code_offset - try_catch.jump_op.code_offset

        # Update any IF-related offsets
        for if_block in self.if_blocks:
            # print ("IF BLOCK START", id(if_block), if_block.if_op, if_block.if_op.code_offset)
            # print ("         END", if_block.end_op, if_block.end_op.code_offset)
            # print ("IF BLOCK JUMP", if_block.jump_op, if_block.jump_op.code_offset)
            # Update the jumps for the initial IF block
            if_block.if_op.offset = if_block.end_op.code_offset - if_block.if_op.code_offset
            if if_block.jump_op:
                if_block.jump_op.offset = if_block.end_op.code_offset - if_block.jump_op.code_offset

            # # Update the jumps for each ELIF/ELSE
            for else_if in if_block.elifs:
                # print('    has elif')
                else_if.if_op.offset = if_block.end_op.code_offset - else_if.if_op.code_offset
                if else_if.jump_op:
                    else_if.jump_op.offset = if_block.end_op.code_offset - else_if.jump_op.code_offset

        # Construct a line number table from
        # the source code reference data on opcodes.
        line_numbers = []
        for code in self.code:
            if code.starts_line is not None:
                line_numbers.append((code.code_offset, code.starts_line))
        line_number_table = LineNumberTable(line_numbers)

        return JavaCode(
            max_stack=self.stack_depth(),
            max_locals=len(self.localvars),
            code=self.code,
            exceptions=exceptions,
            attributes=[
                line_number_table
            ]
        )