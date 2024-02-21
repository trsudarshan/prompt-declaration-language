import json
import os
import types
from pathlib import Path
from typing import Any, Literal, Optional

import requests
import yaml
from dotenv import load_dotenv
from genai.credentials import Credentials
from genai.model import Model
from genai.schemas import GenerateParams

from . import pdl_ast, ui
from .pdl_ast import (
    ApiBlock,
    Block,
    BlockType,
    CallBlock,
    CodeBlock,
    ConditionType,
    ContainsArgs,
    ContainsCondition,
    EndsWithArgs,
    EndsWithCondition,
    ErrorBlock,
    FunctionBlock,
    GetBlock,
    IfBlock,
    InputBlock,
    ModelBlock,
    Program,
    PromptsType,
    PromptType,
    RepeatsBlock,
    RepeatsUntilBlock,
    ScopeType,
    SequenceBlock,
    ValueBlock,
)
from .pdl_dumper import block_to_dict, dump_yaml

# from .pdl_dumper import dump_yaml, dumps_json, program_to_dict

DEBUG = False

load_dotenv()
GENAI_KEY = os.getenv("GENAI_KEY")
GENAI_API = os.getenv("GENAI_API")


empty_scope: ScopeType = {"context": ""}


def generate(
    pdl: str,
    logging: Optional[str],
    mode: Literal["html", "json", "yaml"],
    output: Optional[str],
):
    scope: ScopeType = empty_scope
    if logging is None:
        logging = "log.txt"
    with open(pdl, "r", encoding="utf-8") as infile:
        with open(logging, "w", encoding="utf-8") as logfile:
            data = yaml.safe_load(infile)
            prog = Program.model_validate(data)
            log: list[str] = []
            result = ""
            trace = prog.root
            try:
                result, output, scope, trace = process_block(log, scope, prog.root)
            finally:
                print(result)
                print("\n")
                for prompt in log:
                    logfile.write(prompt)
                if mode == "html":
                    if output is None:
                        output = str(Path(pdl).with_suffix("")) + "_result.html"
                    ui.render(trace, output)
                if mode == "json":
                    if output is None:
                        output = str(Path(pdl).with_suffix("")) + "_result.json"
                    with open(output, "w", encoding="utf-8") as fp:
                        json.dump(block_to_dict(trace), fp)
                if mode == "yaml":
                    if output is None:
                        output = str(Path(pdl).with_suffix("")) + "_result.yaml"
                    with open(output, "w", encoding="utf-8") as fp:
                        dump_yaml(block_to_dict(trace), stream=fp)


def process_block(
    log, scope: ScopeType, block: BlockType
) -> tuple[Any, str, ScopeType, BlockType]:
    if len(block.defs) > 0:
        scope, defs_trace = process_defs(log, scope, block.defs)
    else:
        defs_trace = block.defs
    result, output, scope, trace = process_block_body(log, scope, block)
    if block.assign is not None:
        var = block.assign
        scope = scope | {var: result}
        debug("Storing model result for " + var + ": " + str(trace.result))
    trace = trace.model_copy(update={"defs": defs_trace, "result": output})
    if block.show_result is False:
        output = ""
    scope = scope | {"context": output}
    return result, output, scope, trace


def process_block_body(
    log, scope: ScopeType, block: BlockType
) -> tuple[Any, str, ScopeType, BlockType]:
    scope_init = scope
    result: Any
    output: str
    trace: pdl_ast.BlockType
    match block:
        case ModelBlock():
            result, output, scope, trace = call_model(log, scope, block)
        case CodeBlock(lan="python", code=code):
            result, output, scope, code_trace = call_python(log, scope, code)
            trace = block.model_copy(update={"code": code_trace})
        case CodeBlock(lan=l):
            msg = f"Unsupported language: {l}"
            error(msg)
            result = None
            output = ""
            trace = ErrorBlock(msg=msg, block=block.model_copy())
        case GetBlock(get=var):
            result = get_var(var, scope)
            output = result if isinstance(result, str) else json.dumps(result)
            trace = block.model_copy()
        case ValueBlock(value=v):
            result = v
            output = result if isinstance(result, str) else json.dumps(result)
            trace = block.model_copy()
        case ApiBlock():
            result, output, scope, trace = call_api(log, scope, block)
        case SequenceBlock():
            result, output, scope, prompts = process_prompts(log, scope, block.prompts)
            trace = block.model_copy(update={"prompts": prompts})
        case IfBlock():
            b, _, cond_trace = process_condition(log, scope, block.condition)
            # scope = scope | {"context": scope_init["context"]}
            if b:
                result, output, scope, prompts = process_prompts(
                    log, scope, block.prompts
                )
                trace = block.model_copy(
                    update={
                        "condition": cond_trace,
                        "prompts": prompts,
                    }
                )
            else:
                result = None
                output = ""
                trace = block.model_copy(update={"condition": cond_trace})
        case RepeatsBlock(repeats=n):
            result = None
            output = ""
            iterations_trace: list[PromptsType] = []
            context_init = scope_init["context"]
            for _ in range(n):
                scope = scope | {"context": context_init + output}
                result, iteration_output, scope, prompts = process_prompts(
                    log, scope, block.prompts
                )
                output += iteration_output
                iterations_trace.append(prompts)
            trace = block.model_copy(update={"trace": iterations_trace})
        case RepeatsUntilBlock(repeats_until=cond_trace):
            result = None
            stop = False
            output = ""
            iterations_trace = []
            context_init = scope_init["context"]
            while not stop:
                scope = scope | {"context": context_init + output}
                result, iteration_output, scope, prompts = process_prompts(
                    log, scope, block.prompts
                )
                output += iteration_output
                iterations_trace.append(prompts)
                stop, scope, _ = process_condition(log, scope, cond_trace)
            trace = block.model_copy(update={"trace": iterations_trace})
        case InputBlock():
            result, output, scope, trace = process_input(log, scope, block)
        case FunctionBlock(function=name):
            _ = block.body  # Parse the body of the function
            closure = block.model_copy()
            scope = scope | {name: closure}
            closure.scope = scope
            result = closure
            output = ""
            trace = closure.model_copy(update={})
        case CallBlock(call=f):
            closure = get_var(f, scope)
            f_body = closure.body
            f_scope = closure.scope | {"context": scope["context"]} | block.args
            result, output, _, f_trace = process_block(log, f_scope, f_body)
            trace = block.model_copy(update={"trace": f_trace})
        case _:
            assert False
    return result, output, scope, trace


def process_defs(
    log, scope: ScopeType, defs: list[BlockType]
) -> tuple[ScopeType, list[BlockType]]:
    defs_trace: list[Block] = []
    for b in defs:
        _, _, scope, b_trace = process_block(log, scope, b)
        defs_trace.append(b_trace)
    return scope, defs_trace


def process_prompts(
    log, scope: ScopeType, prompts: PromptsType
) -> tuple[Any, str, ScopeType, PromptsType]:
    result = None
    output: str = ""
    trace: PromptsType = []
    context_init = scope["context"]
    for prompt in prompts:
        scope = scope | {"context": context_init + output}
        result, o, scope, p = process_prompt(log, scope, prompt)
        output += o
        trace.append(p)
    return result, output, scope, trace


def process_prompt(
    log, scope: ScopeType, prompt: PromptType
) -> tuple[Any, str, ScopeType, PromptType]:
    output: str = ""
    if isinstance(prompt, str):
        result = prompt
        output = prompt
        trace = prompt
        append_log(log, "Prompt", prompt)
    elif isinstance(prompt, Block):
        result, output, scope, trace = process_block(log, scope, prompt)
    else:
        assert False
    return result, output, scope, trace


def process_condition(
    log, scope: ScopeType, cond: ConditionType
) -> tuple[bool, ScopeType, ConditionType]:
    trace: ConditionType
    match cond:
        case EndsWithCondition(ends_with=args):
            result, scope, args_trace = ends_with(log, scope, args)
            trace = cond.model_copy(update={"result": result, "ends_with": args_trace})
        case ContainsCondition(contains=args):
            result, scope, args_trace = contains(log, scope, args)
            trace = cond.model_copy(update={"result": result, "contains": args_trace})
        case _:
            result = False
            trace = cond
    return result, scope, trace


def ends_with(
    log, scope: ScopeType, cond: EndsWithArgs
) -> tuple[bool, ScopeType, EndsWithArgs]:
    context_init = scope["context"]
    _, output, scope, arg0_trace = process_prompt(log, scope, cond.arg0)
    scope = scope | {"context": context_init}
    result = output.endswith(cond.arg1)
    return result, scope, cond.model_copy(update={"arg0": arg0_trace})


def contains(
    log, scope: ScopeType, cond: ContainsArgs
) -> tuple[bool, ScopeType, ContainsArgs]:
    context_init = scope["context"]
    _, output, scope, arg0_trace = process_prompt(log, scope, cond.arg0)
    scope = scope | {"context": context_init}
    result = cond.arg1 in output
    return result, scope, cond.model_copy(update={"arg0": arg0_trace})


def call_model(
    log, scope: ScopeType, block: ModelBlock
) -> tuple[Any, str, ScopeType, ModelBlock | ErrorBlock]:
    model_input = ""
    stop_sequences = []
    include_stop_sequences = False
    if block.input is not None:  # If not set to document, then input must be a block
        _, model_input, _, input_trace = process_prompt(log, scope, block.input)
    else:
        input_trace = None
    if model_input == "":
        model_input = scope["context"]
    if block.stop_sequences is not None:
        stop_sequences = block.stop_sequences
    if block.include_stop_sequences is not None:
        include_stop_sequences = block.include_stop_sequences

    if GENAI_API is None:
        msg = "Environment variable GENAI_API must be defined"
        error(msg)
        trace = ErrorBlock(msg=msg, block=block.model_copy())
        return None, "", scope, trace

    if GENAI_KEY is None:
        msg = "Environment variable GENAI_KEY must be defined"
        error(msg)
        trace = ErrorBlock(msg=msg, block=block.model_copy())
        return None, "", scope, trace

    creds = Credentials(GENAI_KEY, api_endpoint=GENAI_API)
    params = None
    if stop_sequences != []:
        params = GenerateParams(  # pyright: ignore
            decoding_method="greedy",
            max_new_tokens=1000,
            min_new_tokens=1,
            # stream=False,
            # temperature=1,
            # top_k=50,
            # top_p=1,
            repetition_penalty=1.07,
            include_stop_sequence=include_stop_sequences,
            stop_sequences=stop_sequences,
        )
    else:
        params = GenerateParams(  # pyright: ignore
            decoding_method="greedy",
            max_new_tokens=1000,
            min_new_tokens=1,
            # stream=False,
            # temperature=1,
            # top_k=50,
            # top_p=1,
            repetition_penalty=1.07,
        )
    try:
        debug("model input: " + model_input)
        append_log(log, "Model Input", model_input)
        model = Model(block.model, params=params, credentials=creds)
        response = model.generate([model_input])
        gen = response[0].generated_text
        debug("model output: " + gen)
        append_log(log, "Model Output", gen)
        trace = block.model_copy(update={"result": gen, "input": input_trace})
        return gen, gen, scope, trace
    except Exception as e:
        msg = f"Model error: {e}"
        error(msg)
        trace = ErrorBlock(
            msg=msg, block=block.model_copy(update={"input": input_trace})
        )
        return None, "", scope, trace


def call_api(
    log, scope: ScopeType, block: ApiBlock
) -> tuple[Any, str, ScopeType, ApiBlock | ErrorBlock]:
    _, input_str, _, input_trace = process_prompt(log, scope, block.input)
    input_str = block.url + input_str
    try:
        append_log(log, "API Input", input_str)
        response = requests.get(input_str)
        result = response.json()
        output = result if isinstance(result, str) else json.dumps(result)
        debug(output)
        append_log(log, "API Output", output)
        trace = block.model_copy(update={"input": input_trace})
    except Exception as e:
        msg = f"API error: {e}"
        error(msg)
        result = None
        output = ""
        trace = ErrorBlock(
            msg=msg, block=block.model_copy(update={"input": input_trace})
        )
    return result, output, scope, trace


def call_python(
    log, scope: ScopeType, code: PromptsType
) -> tuple[Any, str, ScopeType, PromptsType]:
    _, code_str, _, code_trace = get_code_string(log, scope, code)
    my_namespace = types.SimpleNamespace()
    append_log(log, "Code Input", code_str)
    exec(code_str, my_namespace.__dict__)
    result = my_namespace.result
    output = str(result)
    append_log(log, "Code Output", result)
    return result, output, scope, code_trace


def get_code_string(
    log, scope: ScopeType, code: PromptsType
) -> tuple[str, str, ScopeType, PromptsType]:
    _, code_s, _, code_trace = process_prompts(log, scope, code)
    debug("code string: " + code_s)
    return code_s, code_s, scope, code_trace


def process_input(
    log, scope: ScopeType, block: InputBlock
) -> tuple[Any, str, ScopeType, InputBlock | ErrorBlock]:
    if (block.filename is None and block.stdin is False) or (
        block.filename is not None and block.stdin is True
    ):
        msg = "Input block must have either a filename or stdin and not both"
        error(msg)
        trace = ErrorBlock(msg=msg, block=block.model_copy())
        return None, "", scope, trace

    if block.json_content and block.assign is None:
        msg = "If json_content is True in input block, then there must be def field"
        error(msg)
        trace = ErrorBlock(msg=msg, block=block.model_copy())
        return None, "", scope, trace

    if block.filename is not None:
        with open(block.filename, encoding="utf-8") as f:
            s = f.read()
            append_log(log, "Input from File: " + block.filename, s)
    else:  # block.stdin == True
        message = ""
        if block.message is not None:
            message = block.message
        elif block.multiline is False:
            message = "How can I help you?: "
        else:
            message = "Enter/Paste your content. Ctrl-D to save it."
        if block.multiline is False:
            s = input(message)
            append_log(log, "Input from stdin: ", s)
        else:  # multiline
            print(message)
            contents = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                contents.append(line + "\n")
            s = "".join(contents)
            append_log(log, "Input from stdin: ", s)

    if block.json_content and block.assign is not None:
        result = json.loads(s)
        scope = scope | {block.assign: s}
    else:
        result = s

    trace = block.model_copy(update={"result": s})
    return result, s, scope, trace


def get_var(var: str, scope: ScopeType) -> Any:
    segs = var.split(".")
    res = scope[segs[0]]
    for v in segs[1:]:
        res = res[v]
    return res


def append_log(log, title, somestring):
    log.append("**********  " + title + "  **********\n")
    log.append(str(somestring) + "\n")


def debug(somestring):
    if DEBUG:
        print("******")
        print(somestring)
        print("******")


def error(somestring):
    print("***Error: " + somestring)
