from llvmlite import ir, binding
import configparser
import glob
import shutil
import os
import sys
import re
from .data_classes import ArtemisData, TypeEnum
from typing import List, Tuple, Set, Dict, Optional


class ArtemisCompiler:
    def __init__(self, compiler_data: ArtemisData) -> None:
        self.module: ir.Module = ir.Module(name='arx')
        self.module.triple = binding.get_default_triple()
        self.builder : Optional[ir.IRBuilder] = None
        self.func : Optional[ir.Function] = None

        self.compiler_data: ArtemisData = compiler_data

        self.variables : dict = {}
        self.extern_c: List[str] = []
        self.extern_functions: Dict[str, str] = {}

    def load_extern_modules(self, using_modules: List[str]) -> None:
        for path in self.compiler_data.map_paths:
            maps: str = glob.glob(os.path.join(path, '*.map'))
            for map_file in maps:
                cfg: configparser.ConfigParser = configparser.ConfigParser()
                cfg.read(map_file)
                module_name: str = cfg['meta']['name']
                if module_name != 'core':
                    if module_name not in using_modules:
                        continue
                self.extern_c.append(module_name)

                for arx_name, c_name in cfg['functions'].items():
                    self.extern_functions[f'{module_name}.{arx_name}'] = c_name
        self.list_struct_type : ir.IdentifiedStructType = ir.global_context.get_identified_type('List')
        self.list_struct_type.set_body(TypeEnum.int32.as_pointer(), TypeEnum.int32)
    
    def declare_list_len(self) -> ir.Function:
        if not hasattr(self, 'list_len_func'):
            fn_type : ir.FunctionType = ir.FunctionType(TypeEnum.int32, [self.list_struct_type.as_pointer()])
            self.list_len_func : ir.Function = ir.Function(self.module, fn_type, name='core_list_len')
        return self.list_len_func
    
    def call_list_len(self, list_ptr) -> ir.CallInstr:
        list_len_fn : ir.Function = self.declare_list_len()
        return self.builder.call(list_len_fn, [list_ptr])
    
    def declare_list_get(self) -> ir.Function:
        if not hasattr(self, 'list_get_func'):
            fn_type : ir.FunctionType = ir.FunctionType(TypeEnum.int32, [self.list_struct_type.as_pointer(), TypeEnum.int32])
            self.list_get_func : ir.Function = ir.Function(self.module, fn_type, name='core_list_get')
        return self.list_get_func

    def call_list_get(self, list_ptr, index_val):
        list_get_fn = self.declare_list_get()
        return self.builder.call(list_get_fn, [list_ptr, index_val])

    def compile_function(self, name:str, parameters:list, statements:list, return_type:str):
        arg_types : List[ir.Type] = [string_to_ir(parameter_type) for _id, parameter_type, _name in parameters]
        func_type : ir.FunctionType = ir.FunctionType(string_to_ir(return_type), arg_types)
        self.func : ir.Function = ir.Function(self.module, func_type, name=name)
        block : ir.Block = self.func.append_basic_block('entry')
        self.builder : ir.IRBuilder = ir.IRBuilder(block)

        self.variables : dict = {}
        self.current_function_return_type : str = return_type
        for i, (_id, _type, name) in enumerate(parameters):
            arg = self.func.args[i]
            arg.name = name
            ptr = self.builder.alloca(arg.type, name=name)
            self.builder.store(arg, ptr)
            self.variables[name] = ptr

        for statement in statements:
            self.compile_statement(statement)

        if self.builder.block.terminator is None:
            raise Exception(f'Missing return in function {name}')

    def compile_statement(self, statement):
        kind = statement[0]
        if kind == 'expression':
            self.compile_expression(statement[1])
        elif kind == 'return':
            return_value = self.compile_expression(statement[1])
            self.builder.ret(return_value)
        elif kind == 'return_void':
            if self.current_function_return_type != 'void':
                raise TypeError('Void return used in non-void function')
            self.builder.ret_void()
        elif kind == 'declare':
            variable_type_str, variable_name, value_expr = statement[1], statement[2], statement[3]
            value = self.compile_expression(value_expr)

            if variable_type_str == 'int':
                ptr = self.builder.alloca(TypeEnum.int32, name=variable_name)
                self.builder.store(value, ptr)
                self.variables[variable_name] = ptr
            elif variable_type_str == 'bool':
                bool_type : ir.IntType = ir.IntType(1)
                ptr = self.builder.alloca(bool_type, name=variable_name)
                self.builder.store(value, ptr)
                self.variables[variable_name] = ptr
            elif variable_type_str == 'string':
                ptr = self.builder.alloca(TypeEnum.string, name=variable_name)
                self.builder.store(value, ptr)
                self.variables[variable_name] = ptr
            else:
                raise NotImplementedError(f'Unsupported type: {variable_type_str}')
        elif kind == 'if_chain':
            branches = statement[1]
            end_block : ir.Block = self.func.append_basic_block('if_end')
            has_fallthrough : bool = False

            for i, (condition_expression, statements) in enumerate(branches):
                then_block : ir.Block = self.func.append_basic_block(f'if_then_{i}')
                next_block : ir.Block = self.func.append_basic_block(f'if_next_{i}') if i < len(branches) - 1 else end_block

                if condition_expression is not None:
                    cond_val = self.compile_expression(condition_expression)
                    self.builder.cbranch(cond_val, then_block, next_block)
                else:
                    self.builder.branch(then_block)

                self.builder.position_at_start(then_block)

                for statement in statements:
                    self.compile_statement(statement)

                if self.builder.block.terminator is None:
                    self.builder.branch(end_block)
                    has_fallthrough = True

                if condition_expression is not None:
                    self.builder.position_at_start(next_block)

            if has_fallthrough and not end_block.is_terminated:
                self.builder.position_at_start(end_block)
        elif kind == 'for_in':
            var_type, var_name, list_name, body = statement[1], statement[2], statement[3], statement[4]

            index_ptr = self.builder.alloca(TypeEnum.int32, name=f"{var_name}_index")
            self.builder.store(ir.Constant(TypeEnum.int32, 0), index_ptr)

            conditional_block : ir.Block = self.func.append_basic_block('for_cond')
            body_block : ir.Block = self.func.append_basic_block('for_body')
            end_block : ir.Block = self.func.append_basic_block('for_end')

            self.builder.branch(conditional_block)
            self.builder.position_at_start(conditional_block)

            list_ptr = self.variables[list_name]
            index_value = self.builder.load(index_ptr)
            list_len = self.call_list_len(list_ptr)
            cond = self.builder.icmp_signed('<', index_value, list_len)
            self.builder.cbranch(cond, body_block, end_block)

            self.builder.position_at_start(body_block)

            element = self.call_list_get(list_ptr, index_value)
            variable_ptr = self.builder.alloca(TypeEnum.int32, name=var_name)
            self.builder.store(element, variable_ptr)
            self.variables[var_name] = variable_ptr

            for s in body:
                self.compile_statement(s)

            new_index = self.builder.add(index_value, ir.Constant(TypeEnum.int32, 1))
            self.builder.store(new_index, index_ptr)
            self.builder.branch(conditional_block)

            self.builder.position_at_start(end_block)
        elif kind == 'declare_list':
            element_type, name, expression = statement[1], statement[2], statement[3]
            if expression[0] != 'list_literal':
                raise TypeError('Expected list literal')

            elements = [self.compile_expression(e) for e in expression[1]]

            # Build array literal
            array_type : ir.ArrayType = ir.ArrayType(TypeEnum.int32, len(elements))
            array_const : ir.Constant = ir.Constant(array_type, elements)

            array_ptr : ir.AllocaInstr = self.builder.alloca(array_type)
            self.builder.store(array_const, array_ptr)
            casted_ptr = self.builder.bitcast(array_ptr, TypeEnum.int32.as_pointer())

            # Call list_create_int
            create_fn = ir.Function(self.module,
                ir.FunctionType(self.list_struct_type.as_pointer(), [TypeEnum.int32.as_pointer(), TypeEnum.int32]),
                name='core_list_create_int')
            list_ptr = self.builder.call(create_fn, [casted_ptr, ir.Constant(TypeEnum.int32, len(elements))])
            self.variables[name] = list_ptr

    def compile_expression(self, expression):
        kind = expression[0]

        if kind == 'call':
            name : str = expression[1]
            args = expression[2]

            arg_values = [self.compile_expression(arg) for arg in args]
            arg_types = [value.type for value in arg_values]

            func : ir.Function = self.module.globals.get(name)
            if not func:
                func_type : ir.FunctionType = ir.FunctionType(int32, arg_values)
                func : ir.Function = ir.Function(self.module, func_type, name=name)

            return self.builder.call(func, arg_values)
        
        elif kind == 'call_method':
            obj, method, args = expression[1], expression[2], expression[3]
            full_name: str = obj + '.' + method
            if full_name not in self.extern_functions:
                raise NameError(
                    f'Function {full_name} not found in extern functions')

            llvm_data: str = self.extern_functions[full_name]

            # Compile arguments
            arg_vals = [self.compile_expression(arg) for arg in args]
            arg_types = [arg.type for arg in arg_vals]
            llvm_name, return_type_id = llvm_data.split('>')
            if llvm_data.startswith('*') or ':' in llvm_data:
                result: Optional[re.Match] = re.search(
                    rf'([a-zA-Z_][a-zA-Z0-9_]*)\:{','.join([ir_to_string(arg) for arg in arg_types])};', llvm_data)
                if not result:
                    raise TypeError(f'Function {full_name} not does not have ({' '.join(
                        [ir_to_string(arg) for arg in arg_types])}) arguments type match')
                llvm_name = result.group(1)
            return_type: ir.Type = ir.VoidType()
            match return_type_id:
                case 'str':
                    return_type = TypeEnum.string
                case 'int':
                    return_type = TypeEnum.int32
                case 'bool':
                    return_type = TypeEnum.boolean
            # Check if already declared
            func : ir.Function = self.module.globals.get(llvm_name)
            if not func:
                func_type : ir.FunctionType = ir.FunctionType(return_type, arg_types)
                func : ir.Function = ir.Function(self.module, func_type, name=llvm_name)
            return self.builder.call(func, arg_vals)

        elif kind == 'int':
            return ir.Constant(TypeEnum.int32, expression[1])

        elif kind == 'string':
            data : bytearray = bytearray(expression[1].encode('utf8') + b'\0')
            str_type : ir.ArrayType = ir.ArrayType(ir.IntType(8), len(data))
            global_str : ir.GlobalVariable = ir.GlobalVariable(self.module, str_type, name=f'str{
                                           len(self.module.global_values)}')
            global_str.global_constant = True
            global_str.initializer = ir.Constant(str_type, data)
            ptr = self.builder.bitcast(global_str, TypeEnum.string)
            return ptr

        elif kind == 'binop':
            operator, left_part, right_part = expression[1], expression[2], expression[3]
            left_value = self.compile_expression(left_part)
            right_value = self.compile_expression(right_part)

            match operator:
                case '==':
                    if left_value.type == TypeEnum.string and right_value.type == TypeEnum.string:
                        llvm_name: str = 'core_string_equal'
                        func : ir.Function = self.module.globals.get(llvm_name)
                        if not func:
                            func = ir.Function(self.module, ir.FunctionType(TypeEnum.boolean, [
                                            TypeEnum.string, TypeEnum.string]), name=llvm_name)
                        return self.builder.call(func, [left_value, right_value])
                    return self.builder.icmp_signed('==', left_value, right_value)
                case '!=':
                    return self.builder.icmp_signed('!=', left_value, right_value)
                case '<=':
                    return self.builder.icmp_signed('<=', left_value, right_value)
                case '>=':
                    return self.builder.icmp_signed('>=', left_value, right_value)
                case '<':
                    return self.builder.icmp_signed('<', left_value, right_value)
                case '>':
                    return self.builder.icmp_signed('>', left_value, right_value)
                case '+':
                    if left_value.type == TypeEnum.string and right_value.type == TypeEnum.string:
                        llvm_name: str = 'core_string_concat'
                        func : ir.Function = self.module.globals.get(llvm_name)
                        if not func:
                            func = ir.Function(self.module, ir.FunctionType(TypeEnum.string, [
                                            TypeEnum.string, TypeEnum.string]), name=llvm_name)
                        return self.builder.call(func, [left_value, right_value])
                    return self.builder.add(left_value, right_value)
                case '-':
                    return self.builder.sub(left_value, right_value)
                case '*':
                    return self.builder.mul(left_value, right_value)
                case '/':
                    return self.builder.sdiv(left_value, right_value)  # signed division
                case _:
                    raise NotImplementedError(f'Unsupported operator: {operator}')

        elif kind == 'var':
            var_name = expression[1]
            if var_name not in self.variables:
                raise NameError(f'Undefined variable: {var_name}')
            ptr = self.variables[var_name]
            return self.builder.load(ptr)

        elif kind == 'bool':
            return ir.Constant(ir.IntType(1), 1 if expression[1] else 0)

        else:
            raise NotImplementedError(f'Expresion kind {kind} not implemented')

    def add_c_main(self):
        func_type : ir.FunctionType = ir.FunctionType(TypeEnum.int32, [])
        main_fn : ir.Function = ir.Function(self.module, func_type, name='main')
        block : ir.Block = main_fn.append_basic_block(name='entry')
        builder : ir.IRBuilder = ir.IRBuilder(block)

        exec_fn = self.module.get_global('_exec')
        return_value = builder.call(exec_fn, [])
        builder.ret(return_value)

def string_to_ir(string_type: str) -> ir.Type:
    match string_type:
        case 'int':
            return TypeEnum.int32
        case 'bool':
            return TypeEnum.boolean
        case 'str':
            return TypeEnum.string
        case _:
            pass
    return TypeEnum.void

def ir_to_string(ir_type: ir.Type) -> str:
    if isinstance(ir_type, ir.IntType):
        if ir_type.width == 1:
            return 'bool'
        elif ir_type.width == 32:
            return 'int'
    elif isinstance(ir_type, ir.PointerType):
        return 'str'
    return 'void'
