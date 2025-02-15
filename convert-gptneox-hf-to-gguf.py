#!/usr/bin/env python3
# HF gptneox--> gguf conversion

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import torch
from transformers import AutoTokenizer  # type: ignore[import]

if 'NO_LOCAL_GGUF' not in os.environ:
    sys.path.insert(1, str(Path(__file__).parent / 'gguf-py' / 'gguf'))
import gguf

# ref: https://github.com/openai/gpt-2/blob/master/src/encoder.py

## Taken from conver.py
class BpeVocab:
    def __init__(self, fname_tokenizer: Path, fname_added_tokens: Path | None) -> None:
        self.bpe_tokenizer = json.loads(open(str(fname_tokenizer), encoding="utf-8").read())["model"]["vocab"]
        added_tokens: dict[str, int]
        if fname_added_tokens is not None:
            # FIXME: Verify that added tokens here _cannot_ overlap with the main vocab.
            added_tokens = json.load(open(fname_added_tokens, encoding="utf-8"))
        else:
            # Fall back to trying to find the added tokens in tokenizer.json
            tokenizer_json_file = fname_tokenizer.parent / 'tokenizer.json'
            if not tokenizer_json_file.is_file():
                added_tokens = {}
            else:
                tokenizer_json = json.load(open(tokenizer_json_file, encoding="utf-8"))
                added_tokens = dict(
                    (item['content'], item['id'])
                    for item in tokenizer_json.get('added_tokens', [])
                    # Added tokens here can be duplicates of the main vocabulary.
                    if item['content'] not in self.bpe_tokenizer )

        vocab_size: int = len(self.bpe_tokenizer)
        expected_ids    = list(range(vocab_size, vocab_size + len(added_tokens)))
        actual_ids      = sorted(added_tokens.values())
        if expected_ids != actual_ids:
            expected_end_id = vocab_size + len(actual_ids) - 1
            raise Exception(f"Expected the {len(actual_ids)} added token ID(s) to be sequential in the range {vocab_size} - {expected_end_id}; got {actual_ids}")

        items = sorted(added_tokens.items(), key=lambda text_idx: text_idx[1])
        self.added_tokens_list    = [text for (text, idx) in items]
        self.vocab_size_base: int = vocab_size
        self.vocab_size: int      = self.vocab_size_base + len(self.added_tokens_list)
        self.fname_tokenizer      = fname_tokenizer
        self.fname_added_tokens   = fname_added_tokens

    def bpe_tokens(self) -> Iterable[tuple[bytes, float, gguf.TokenType]]:
        tokenizer = self.bpe_tokenizer
        from transformers.models.gpt2 import tokenization_gpt2  # type: ignore[import]
        # byte_encoder = tokenization_gpt2.bytes_to_unicode()
        byte_encoder = bytes_to_unicode()
        byte_decoder = {v: k for k, v in byte_encoder.items()}
        score = 0.0
        for i, item in enumerate(tokenizer):
            text: bytes = item.encode("utf-8")
            # FIXME: These shouldn't be hardcoded, but it's probably better than the current behavior?
            if i <= 258 and text.startswith(b'<') and text.endswith(b'>'):
                if i == 0 and text == b'<unk>':
                    toktype = gguf.TokenType.UNKNOWN
                elif i == 1 or i == 2:
                    toktype = gguf.TokenType.CONTROL
                elif i >= 3 and text.startswith(b'<0x'):
                    toktype = gguf.TokenType.BYTE
                else:
                    toktype = gguf.TokenType.NORMAL
            else:
                toktype = gguf.TokenType.NORMAL
            yield text, score, toktype

    def added_tokens(self) -> Iterable[tuple[bytes, float, gguf.TokenType]]:
        for text in self.added_tokens_list:
            score = -1000.0
            yield text.encode("utf-8"), score, gguf.TokenType.USER_DEFINED

    def all_tokens(self) -> Iterable[tuple[bytes, float, gguf.TokenType]]:
        yield from self.bpe_tokens()
        yield from self.added_tokens()

    def __repr__(self) -> str:
        return f"<BpeVocab with {self.vocab_size_base} base tokens and {len(self.added_tokens_list)} added tokens>"

def bytes_to_unicode():
    """
    Returns list of utf-8 byte and a corresponding list of unicode strings.
    The reversible bpe codes work on unicode strings.
    This means you need a large # of unicode characters in your vocab if you want to avoid UNKs.
    When you're at something like a 10B token dataset you end up needing around 5K for decent coverage.
    This is a significant percentage of your normal, say, 32K bpe vocab.
    To avoid that, we want lookup tables between utf-8 bytes and unicode strings.
    And avoids mapping to whitespace/control characters the bpe code barfs on.
    """
    bs = list(range(ord("!"), ord("~")+1))+list(range(ord("¡"), ord("¬")+1))+list(range(ord("®"), ord("ÿ")+1))
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8+n)
            n += 1
    return dict(zip(bs, (chr(n) for n in cs)))


def count_model_parts(dir_model: Path) -> int:
    num_parts = 0
    for filename in os.listdir(dir_model):
        if filename.startswith("pytorch_model-"):
            num_parts += 1

    if num_parts > 0:
        print("gguf: found " + str(num_parts) + " model parts")
    return num_parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a GPT-NeoX model to a GGML compatible file")
    parser.add_argument("--vocab-only",  action="store_true",    help="extract only the vocab")
    parser.add_argument("--outfile",     type=Path,              help="path to write to; default: based on input")
    parser.add_argument("model",         type=Path,              help="directory containing model file, or model file itself (*.bin)")
    parser.add_argument("ftype",     type=int, choices=[0, 1],   help="output format - use 0 for float32, 1 for float16", default = 1)
    return parser.parse_args()

args = parse_args()

dir_model = args.model
ftype = args.ftype
if not dir_model.is_dir():
    print(f'Error: {args.model} is not a directory', file = sys.stderr)
    sys.exit(1)

# possible tensor data types
#   ftype == 0 -> float32
#   ftype == 1 -> float16

# map from ftype to string
ftype_str = ["f32", "f16"]

if args.outfile is not None:
    fname_out = args.outfile
else:
    # output in the same directory as the model by default
    fname_out = dir_model / f'ggml-model-{ftype_str[ftype]}.gguf'

print("gguf: loading model "+dir_model.name)

with open(dir_model / "config.json", "r", encoding="utf-8") as f:
    hparams = json.load(f)

if hparams["architectures"][0] != "GPTNeoXForCausalLM":
    print("Model architecture not supported: " + hparams["architectures"][0])

    sys.exit()

# get number of model parts
num_parts = count_model_parts(dir_model)

ARCH=gguf.MODEL_ARCH.GPTNEOX
gguf_writer = gguf.GGUFWriter(fname_out, gguf.MODEL_ARCH_NAMES[ARCH])

print("gguf: get model metadata")

block_count = hparams["num_hidden_layers"]

gguf_writer.add_name(dir_model.name)
gguf_writer.add_context_length(hparams["max_position_embeddings"])
gguf_writer.add_embedding_length(hparams["hidden_size"])
gguf_writer.add_block_count(block_count)
gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
gguf_writer.add_rope_dimension_count(int(hparams["rotary_pct"]*(hparams["hidden_size"]//hparams["num_attention_heads"])))
gguf_writer.add_head_count(hparams["num_attention_heads"])
gguf_writer.add_parallel_residual(hparams["use_parallel_residual"] if "use_parallel_residual" in hparams else True)
gguf_writer.add_layer_norm_eps(hparams["layer_norm_eps"])

# TOKENIZATION

print("gguf: get tokenizer metadata")

tokens: list[bytearray] = []

tokenizer_json_file = dir_model / 'tokenizer.json'
if not tokenizer_json_file.is_file():
    print(f'Error: Missing {tokenizer_json_file}', file = sys.stderr)
    sys.exit(1)

# gpt2 tokenizer
gguf_writer.add_tokenizer_model("gpt2")

with open(tokenizer_json_file, "r", encoding="utf-8") as f:
    tokenizer_json = json.load(f)

print("gguf: get gpt2 tokenizer vocab")

vocab_size = len(tokenizer_json["model"]["vocab"])

# ref: https://github.com/cmp-nct/ggllm.cpp/blob/master/falcon_convert.py
# tokenizer = AutoTokenizer.from_pretrained(dir_model)

# reverse_vocab = {id: encoded_tok for encoded_tok, id in tokenizer.vocab.items()}
# byte_encoder = bytes_to_unicode()
# byte_decoder = {v: k for k, v in byte_encoder.items()}

# for i in range(vocab_size):
#     if i in reverse_vocab:
#         try:
#             text = bytearray([byte_decoder[c] for c in reverse_vocab[i]])
#         except KeyError:
#             text = bytearray()
#             for c in reverse_vocab[i]:
#                 if ord(c) < 256:  # single byte character
#                     text.append(byte_decoder[ord(c)])
#                 else:  # multibyte special token character
#                     text.extend(c.encode('utf-8'))
#     else:
#         print(f"Key {i} not in tokenizer vocabulary. Padding with an arbitrary token.")
#         pad_token = f"[PAD{i}]".encode("utf8")
#         text = bytearray(pad_token)

#     tokens.append(text)

# gguf_writer.add_token_list(tokens)

added_tokens_path = dir_model / 'added_tokens.json'
vocab = BpeVocab(dir_model / 'tokenizer.json', added_tokens_path if added_tokens_path.exists() else None)
# add scores
tokens = []
scores = []
toktypes = []
# NOTE: `all_tokens` returns the base vocabulary and added tokens
for text, score, toktype in vocab.all_tokens():
    tokens.append(text)
    scores.append(score)
    toktypes.append(toktype)

gguf_writer.add_token_list(tokens)
gguf_writer.add_token_scores(scores)
gguf_writer.add_token_types(toktypes)
special_vocab = gguf.SpecialVocab(dir_model, load_merges = True)
special_vocab.add_to_gguf(gguf_writer)
# TENSORS

tensor_map = gguf.get_tensor_name_map(ARCH,block_count)

# tensor info
print("gguf: get tensor metadata")

if num_parts == 0:
    part_names = iter(("pytorch_model.bin",))
else:
    part_names = (
        f"pytorch_model-{n:05}-of-{num_parts:05}.bin" for n in range(1, num_parts + 1)
    )

for part_name in part_names:
    if args.vocab_only:
        break
    print("gguf: loading model part '" + part_name + "'")
    model_part = torch.load(f"{dir_model}/{part_name}", map_location="cpu")

    for name in model_part.keys():
        data = model_part[name]

        # we don't need these
        if name.endswith(".attention.masked_bias") or name.endswith(".attention.bias") or name.endswith(".attention.rotary_emb.inv_freq"):
            continue

        old_dtype = data.dtype

        # convert any unsupported data types to float32
        if data.dtype != torch.float16 and data.dtype != torch.float32:
            data = data.to(torch.float32)

        data = data.squeeze().numpy()

        # map tensor names
        new_name = tensor_map.get_name(name, try_suffixes = (".weight", ".bias"))
        if new_name is None:
            print("Can not map tensor '" + name + "'")
            sys.exit()

        n_dims = len(data.shape)
        data_dtype = data.dtype

        # if f32 desired, convert any float16 to float32
        if ftype == 0 and data_dtype == np.float16:
            data = data.astype(np.float32)

        # TODO: Why cant we use these float16 as-is? There should be not reason to store float16 as float32
        if ftype == 1 and data_dtype == np.float16 and n_dims == 1:
            data = data.astype(np.float32)

        # if f16 desired, convert any float32 2-dim weight tensors to float16
        if ftype == 1 and data_dtype == np.float32 and name.endswith(".weight") and n_dims == 2:
            data = data.astype(np.float16)

        print(new_name + ", n_dims = " + str(n_dims) + ", " + str(old_dtype) + " --> " + str(data.dtype))

        gguf_writer.add_tensor(new_name, data)


print("gguf: write header")
gguf_writer.write_header_to_file()
print("gguf: write metadata")
gguf_writer.write_kv_data_to_file()
if not args.vocab_only:
    print("gguf: write tensors")
    gguf_writer.write_tensors_to_file()

gguf_writer.close()

print(f"gguf: model successfully exported to '{fname_out}'")
print("")
