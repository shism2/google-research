# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Types storing a chunk of text in raw (token IDs) or processed (vectors) form.

Type `Chunk` stores token IDs, and is typically used as the input of a model.
This is the "unprocessed" representation of text.

Type `FullChunkResult` stores the "full" outputs of the model on a `Chunk`: the
KV caches and the per-token logits. This is often very big: for example on
PaLM-8B the KV cache is 16KiB per token, and the per-token logits are 1MiB
per token. Because this type is big, we prefer not to return it to the host e.g.
on JIT boundaries. Instead, we prefer to only use it for the internals of the
model.

Type `ChunkResult` is a reduced version of `FullChunkResult` that is small
enough to be returned at JIT boundaries. It contains the KV cache, the top few
highest-probability logits for each token, and the full set of logits for the
last token. (On PaLM-8B, the KV cache is 16KiB per token, the
highest-probability logits are 64B per token, and the final-token logits are
1MiB but only on one token.) The `ChunkResult` has sufficient information in it
for most scoring use cases (getting per-token or per-sequence scores) as well as
for generation use cases.

Type `InferFn` is a function type that we expect model implementations to
provide. It represents a single forwards pass through a model, processing input
tokens in a `Chunk` and returning a `FullChunkResult` that corresponds to the
input tokens.

##### Splitting text into chunks

When preparing inputs to a model, you can put all the text in a single chunk, or
in multiple different chunks. For example, all the text in a single chunk:

```
# A chunk with batch size 2.
chunk = Chunk.tokenize(
    vocab,
    ["Humans have 2 legs.",
     "Humans have 3 legs.",
    ],
    is_first_chunk=True)
```

Alternatively, text split over multiple chunks:

```
# Prefix has batch size 1.
prefix_chunk = Chunk.tokenize(vocab, ["Humans have"], is_first_chunk=True)
# Suffix has batch size 2.
suffix_chunk = Chunk.tokenize(vocab, ["2 legs.", "3 legs."],
                              is_first_chunk=False)
chunks = [prefix_chunk, suffix_chunk]
```

Here, `chunk` and `chunks` represent the same set of two sentences, but we
have split them into chunks differently. In particular, in the second
representation we have taken advantage of the common prefix "Humans have" and
stored it only once, rather than twice. This can make the model more efficient
when processing `chunks`. For example, compare inference on `chunk` vs `chunks`:

```
infer: InferFn   # From somewhere
weights: Weights  # From somewhere

# Processing `chunk` by itself
chunk_result = infer(weights, [], chunk)

# Processing `chunks`, in two infer calls.
prefix_result = infer(weights, [], prefix_chunk)
suffix_result = infer(weights, [prefix_result.kv_cache], suffix_chunk)
```

In this example, when processing `chunk`, the `infer` function must redundantly
run the model on the "Humans have" tokens twice: once for each batch element. In
contrast, when processing `chunks`, the first `infer` call processes the
"Humans have" tokens just once, and then in the second `infer` call this
processing is shared across both batch elements "2 legs." and "3 legs.".
"""

from typing import Any, Optional, Sequence, Tuple, Union

from flax import struct
import jax
from jax import lax
from jax.experimental.pjit import PartitionSpec as P
import jax.numpy as jnp
import jax.scipy
import numpy as np
import seqio
import typing_extensions

from scaling_transformer_inference_efficiency import attention
from scaling_transformer_inference_efficiency import checkpoint
from scaling_transformer_inference_efficiency import partitioning
from scaling_transformer_inference_efficiency import special2

Weights = Any

_BOS_ID = 0


@struct.dataclass
class Chunk:
  """A chunk of token IDs. These are typically used as the input to a model."""
  tokens: Union[np.ndarray, jnp.ndarray]  # int32[batch, max_len]
  lengths: Union[np.ndarray, jnp.ndarray]  # int32[batch]

  @classmethod
  def logical_axes(cls):
    return Chunk(
        tokens=P('batch', 'time'),
        lengths=P('batch'),
    )

  @classmethod
  def zeros(cls, batch, seqlen):
    """Returns an all-zeros Chunk of the specified shape.

    The returned Chunk doesn't have a useful meaning as text. This function is
    primarily intended to be used as initial loop state, which will subsequently
    be overwritten with meaningful token IDs.

    Args:
      batch: batch size.
      seqlen: number of tokens in the chunk.

    Returns:
      A Chunk with zeros in all locations.
    """
    return Chunk(
        tokens=jnp.zeros((batch, seqlen), jnp.int32),
        lengths=jnp.zeros((batch,), jnp.int32),
    )

  @classmethod
  def tokenize(cls, vocab, texts,
               is_first_chunk):
    """Parses the text into token IDs and creates a Chunk from them.

    For example:

    ```
    # A chunk with batch size 2.
    chunk = Chunk.tokenize(
        vocab,
        ["Humans have 2 legs.",
        "Humans have 3 legs.",
        ],
        is_first_chunk=True)
    ```

    Alternatively, the same text split over multiple chunks:

    ```
    # Prefix has batch size 1.
    prefix_chunk = Chunk.tokenize(vocab, ["Humans have"], is_first_chunk=True)
    # Suffix has batch size 2.
    suffix_chunk = Chunk.tokenize(vocab, ["2 legs.", "3 legs."],
                                  is_first_chunk=False)
    chunks = [prefix_chunk, suffix_chunk]
    ```

    Args:
      vocab: The vocabulary with which to parse the text into tokens.
      texts: The batch of sequences to parse.
      is_first_chunk: Whether this is the first chunk in a logical sequence. If
        so, as part of tokenization we will prepend the special
        "beginning-of-sequence" token (token ID = 0), which informs the model
        that this is indeed the beginning of the sequence.

    Returns:
      The batch of sequences, parsed into a Chunk. The result's sequence length
      equals the longest sequence length of any input string.
    """
    # t5x/models.py;l=643
    # t5x/google/prediction_service/handler.py;l=514
    # t5x/google/prediction_service/handler.py;l=425
    # seqio/dataset_providers.py;l=1106
    #   - task evaluation
    # seqio/dataset_providers.py;l=943
    #   - preprocessor evaluation
    # prediction_service.gin;l=41
    #   - Gin config
    # seqio.DecoderFeatureConverter

    # First we:
    # * parse the strings into token IDs
    # * pad all the sequences so their lengths equal the longest sequences's
    #   length, and form a batch
    lengths = []  # List[int]
    batch_tokens = []  # List[int32[seqlen]]. Each seqlen can be different.
    max_length = 0
    for text in texts:
      # Parsing:
      tokens = vocab.encode_tf(text)
      length, = tokens.shape
      if length > max_length:
        max_length = length
      lengths.append(length)
      batch_tokens.append(np.array(tokens))

    # Padding to max length, and then concatenating into a batch
    batch_tokens = np.array([
        np.pad(
            tokens, (0, max_length - tokens.shape[0]),
            constant_values=vocab.pad_id) for tokens in batch_tokens
    ])
    # ^ batch_tokens: int32[batch, seqlen]
    lengths = np.array(lengths)
    # ^ lengths: int32[batch]

    # The model expects a beginning-of-sequence token (id equal to _BOS_ID) at
    # the beginning of the logical string of text. If this is the first chunk in
    # a list, we should add it. Otherwise, if it's a later chunk in the list,
    # then the beginning-of-sequence token has already been added to the first
    # one, so we don't need it again.
    if is_first_chunk:
      batch_tokens = jnp.concatenate([
          jnp.full((batch_tokens.shape[0], 1), _BOS_ID, jnp.int32), batch_tokens
      ],
                                     axis=1)
      lengths = lengths + 1

    # After padding and beginning-of-sequence insertion, an example output would
    # be:
    #
    # batch_tokens:
    # [[0, 123, 456, 789, 0,     0],
    #  [0, 234, 567, 890, 123, 456],
    # ]
    # lengths:
    # [4, 6]
    #
    # Alternatively, for the same text but without beginning-of-sequence
    # insertion:
    #
    # batch_tokens:
    # [[123, 456, 789, 0,     0],
    #  [234, 567, 890, 123, 456],
    # ]
    # lengths:
    # [3, 5]
    return Chunk(
        tokens=batch_tokens,
        lengths=lengths,
    )

  def detokenize(self, vocab):
    """Turns a chunk back into text.

    ```
    orig_texts = ["Humans have 2 legs.",
        "Humans have 3 legs.",
        ]
    chunk = Chunk.tokenize(vocab, orig_texts, is_first_chunk=True)
    texts = chunk.detokenize(vocab)
    assert(texts == orig_texts)
    ```

    Args:
      vocab: Vocabulary for detokenization.

    Returns:
      Text form of the chunk.
    """
    me = self.copy_to_host()
    # Mask out everything above 'lengths', by replacing it with the
    # end-of-sequence token ID. Then vocab.decode_tf won't decode past the
    # end-of-sequence token.
    masked_tokens = np.where(
        np.array(me.token_mask), np.array(me.tokens), vocab.eos_id)
    return list(vocab.decode_tf(masked_tokens).numpy())

  @property
  def token_mask(self):
    """Gets a mask which is true for in-bounds tokens. bool[batch, seqlen]."""
    token_index = jax.lax.broadcasted_iota(jnp.int32, self.tokens.shape, 1)
    return token_index < self.lengths[:, np.newaxis]

  def copy_to_host(self):
    """Copies the data from the device to the host."""
    return Chunk(np.array(self.tokens), np.array(self.lengths))

  def split_at(self, n):
    """Splits a chunk into two chunks, where the first has length `n`."""
    assert n < self.tokens.shape[1]
    me = self.copy_to_host()
    first = Chunk(me.tokens[:, :n], np.minimum(me.lengths, n))
    second = Chunk(me.tokens[:, n:], np.maximum(me.lengths, n) - n)
    return first, second

  def pad_to_length(self, n):
    """Pads the chunk to the target length."""
    seqlen = self.tokens.shape[1]
    assert n >= seqlen
    tokens = jnp.pad(self.tokens, ((0, 0), (0, n - seqlen)))
    return Chunk(tokens, self.lengths)

  def update(self, token_i, token):
    """Writes the batch of tokens to the specified token index."""
    assert token.tokens.shape[1] == 1, 'token must have seqlen=1'
    return Chunk(
        tokens=lax.dynamic_update_index_in_dim(self.tokens, token.tokens[:, 0],
                                               token_i, 1),
        lengths=jnp.maximum(self.lengths, token_i + 1))


@struct.dataclass
class ChunkResult:
  """Result of analyzing a `Chunk` by the neural net.

  This is returned at JIT boundaries.
  """
  # Scores and other candidates for the _current_ token (not the next one).
  per_token_scores: jnp.ndarray  # float32[batch, seqlen]
  top_token_ids: jnp.ndarray  # int32[batch, seqlen, top_k]
  top_token_probs: jnp.ndarray  # float32[batch, seqlen, top_k]

  # Logits for the _next_ token
  next_token_logits: jnp.ndarray  # float32[batch, vocab_size]

  # KV cache.
  kv_cache: attention.KVCache

  @classmethod
  def logical_axes(cls):
    return ChunkResult(
        per_token_scores=P('batch', 'time'),
        top_token_ids=P('batch', 'time', 'top_k'),
        top_token_probs=P('batch', 'time', 'top_k'),
        next_token_logits=P('logit_batch', 'vocab'),
        kv_cache=attention.KVCache.logical_axes(),
    )

  def copy_to_host(self):
    return jax.tree_map(jax.device_get, self)

  @classmethod
  def zeros(cls, hparams, batch,
            seqlen):
    """Creates an all-zeros ChunkResult of the specified shape."""
    return ChunkResult(
        kv_cache=attention.KVCache.zeros(hparams, batch, seqlen),
        per_token_scores=jnp.zeros((batch, seqlen), jnp.float32),
        top_token_ids=jnp.zeros((batch, seqlen, _TOP_K), jnp.int32),
        top_token_probs=jnp.zeros((batch, seqlen, _TOP_K), jnp.float32),
        next_token_logits=jnp.zeros((batch, hparams.vocab), jnp.float32),
    )

  def update(self,
             token_i,
             token_chunk,
             token_full_result,
             per_device = False):
    """Writes a single-token FullChunkResult to the specified index of this.

    The index token_i is assumed to be the last token written to this
    ChunkResult so far.

    Args:
      token_i: The seqlen index to write to.
      token_chunk: The input tokens with which to write. Shape Chunk[batch, 1].
      token_full_result: The results to write. Shape FullChunkResult[batch, 1].
      per_device: Whether this is used in a per device or global context.
    Returns:
      This, but with token written at index token_i.
    """
    token_batch, token_seqlen, token_vocab = token_full_result.logits.shape
    batch, vocab = self.next_token_logits.shape
    assert batch == token_batch
    assert token_seqlen == 1
    assert token_vocab == vocab

    token_small = token_full_result.to_chunk_result(self.next_token_logits,
                                                    token_chunk, per_device)

    return ChunkResult(
        kv_cache=self.kv_cache.write_token(token_i, token_full_result.kv_cache),
        per_token_scores=lax.dynamic_update_index_in_dim(
            self.per_token_scores, token_small.per_token_scores[:, 0], token_i,
            1),
        top_token_ids=lax.dynamic_update_index_in_dim(
            self.top_token_ids, token_small.top_token_ids[:, 0, :], token_i, 1),
        top_token_probs=lax.dynamic_update_index_in_dim(
            self.top_token_probs, token_small.top_token_probs[:, 0, :], token_i,
            1),
        next_token_logits=token_full_result.logits[:, 0, :],
    )


_TOP_K = 4
_BOS_ID = 0


def _bos_logits(vocab_size):
  """Logits that put assign probability 1.0 to on _BOS_ID."""
  logits = jnp.full((vocab_size,), -1e10)
  return logits.at[_BOS_ID].set(0.0)


@struct.dataclass
class FullChunkResult:
  """Result produced by an 'infer' call."""
  logits: jnp.ndarray  # float32[batch, seqlen, vocab_size]
  kv_cache: attention.KVCache

  @classmethod
  def logical_axes(cls):
    return FullChunkResult(
        logits=P('logit_batch', 'time', 'vocab'),
        kv_cache=attention.KVCache(
            lengths=P(None),
            k=P('length', 'layers', 'attn_batch', 'qkv'),
            v=P('length', 'layers', 'attn_batch', 'qkv')))

  def to_chunk_result(self,
                      prev_logits,
                      chunk,
                      per_device = False):
    """Converts this to its more minimal form, ChunkResult.

    Args:
      prev_logits: The `next_token_logits` of the previous chunk in the
        sequence, or None if this is the first chunk in the sequence.
        float32[batch, vocab_size]. In 2D [batch.x, time, vocab.yz]
      chunk: Input token IDs for this chunk.
      per_device: Whether this is used in a per device or global context.

    Returns:
      This, but in its minimized form.
    """
    # Example 1 (first chunk in a sequence):
    #
    #   prev_logits = None
    #   tokens = [0, 123, 456, 789]
    #   self.logits = [logits_123, logits_456, logits_789, logits_next]
    #
    # Here `logits_123` is a set of logits that assigns a reasonably high
    # probability to the token ID 123. Note that `self.logits`` is shifted left
    # by 1 from `tokens`, because `self.logits` is predicting the next token.
    #
    # We compute scores for the 4 tokens we've seen, by shifting `self.logits`
    # right by 1. We need a probability distribution for the first token. Since
    # the first token in the sequence must always be the beginning-of-sequence
    # token (ID=0), we use the special `_bos_logits` for the first token, which
    # assigns probability 1.0 to ID=0.
    #
    # So we compute per-token scores as:
    #
    #  shifted_logits = [_bos_logits, logits_123, logits_456, logits_789]
    #  per_token_scores = shifted_logits[self.logits]
    #    = [_bos_logits[0], logits_123[123], logits_456[456], logits_789[789]]
    #
    # The values `logits_next` have "fallen off the end" of the chunk. They are
    # not useful in computing scores for this chunk, but we remember them
    # because we'll use them to compute scores for the beginning of the next
    # chunk. We store this in `ChunkResult.next_token_logits`.
    #
    # Example 2 (second chunk in a sequence):
    #
    #   prev_logits = <some jnp.ndarray>
    #   tokens = [987, 654, 321]
    #   self.logits = [logits_654, logits_321, logits_next]
    #
    # This time when computing `shifted_logits`, we don't use `_bos_logits`.
    # Instead we use `prev_chunk.next_token_logits`. That yields:
    #
    #   shifted_logits = [prev_logits, logits_654, logits_321]
    #   per_token_scores = shifted_logits[self.logits]
    #    = [prev_logits[987], logits_654[654], logits_321[321]]
    #
    # Example 3 (second chunk in a sequence is empty):
    #
    #   prev_chunk = <some ChunkResult>
    #   tokens = []
    #   self.logits = []
    #
    # This is mostly degenerate but there's an important special case we need
    # to handle: `ChunkResult.next_token_logits` doesn't come from
    # `self.logits[-1]` like usual (because that would be empty); instead it
    # comes from `prev_chunk.next_token_logits`.

    batch, seqlen, vocab_size = self.logits.shape

    # we need to slice into lengths because logits is sharded
    if per_device:
      logit_sharding_axis = partitioning.get_sharding_divisor(P('logit_batch'))
      # TODO(sholto): This assumes logit batch is only ever sharded along x
      #               or is unsharded
      physical_logit_batch = partitioning.logical_to_physical(P('logit_batch'))
      if 'y' in physical_logit_batch or 'z' in physical_logit_batch:
        raise NotImplementedError('Only one sampling partitioning implemented.')
      x_index = lax.axis_index('x') * logit_sharding_axis
      lengths = lax.dynamic_slice_in_dim(
          chunk.lengths,
          x_index * batch,  # batch is per device
          batch)
    else:
      lengths = chunk.lengths

    # First figure out what logits to use for the first token.
    if prev_logits is None:
      # Use beginning-of-sequence marker as the logits.
      prev_logits = jnp.broadcast_to(
          _bos_logits(vocab_size), (batch, vocab_size))
      # ^ prev_logits: f32[batch, vocab]
    else:
      prev_logits = attention.flat_broadcast(prev_logits, batch)
      # ^ prev_logits: f32[batch, vocab]

    # Now shift in the prev_logits and shift out the last token's logits.
    shifted_logits = jnp.concatenate(
        [prev_logits[:, np.newaxis, :], self.logits[:, :-1, :]], axis=1)
    batch_iota = lax.broadcasted_iota(jnp.int32, (batch,), 0)
    next_token_logits = self.logits[batch_iota, lengths - 1, :]
    # ^ next_token_logits: f32[batch, vocab]
    length_is_zero = lengths == 0
    # ^ length_is_zero: f32[batch]
    length_is_zero = length_is_zero[:, np.newaxis]
    # length_is_zero: bool[batch, 1]
    # Special handling for the case where the sequence length is zero, see
    # Example 3 above.
    next_token_logits = jnp.where(length_is_zero, prev_logits,
                                  next_token_logits)

    if per_device:
      # To do this we'd need to do some collective ops,
      # these are unnecessary auxiliary outputs for the moment, so
      # just zero them out.
      # TODO(sholto): Ideally put these somewhere in sampling,
      #       where we have the fully materialised vocab dim
      global_batch = batch * logit_sharding_axis
      per_token_scores = jnp.zeros((global_batch, seqlen), jnp.float32)
      top_ids = jnp.zeros((global_batch, seqlen, _TOP_K), jnp.int32)
      top_probs = jnp.zeros((global_batch, seqlen, _TOP_K), jnp.float32)

    else:
      # Now compute the compressed representation of shifted_logits, extracting
      # per-token scores, and per-token top token IDs.
      batch_iota = lax.broadcasted_iota(jnp.int32, (batch, seqlen), 0)
      token_iota = lax.broadcasted_iota(jnp.int32, (batch, seqlen), 1)
      logits_max = jnp.max(shifted_logits, axis=-1)
      logits_sumexp = jnp.sum(
          special2.exp2(shifted_logits - logits_max[:, :, np.newaxis]), axis=-1)
      logits_sum = jnp.log2(logits_sumexp) + logits_max
      per_token_scores = (shifted_logits[batch_iota, token_iota, chunk.tokens] -
                          logits_sum) * special2.LN_2
      top_logits, top_ids = lax.top_k(shifted_logits, k=_TOP_K)

      top_probs = special2.exp2(top_logits - logits_max[:, :, np.newaxis]) * (
          1.0 / logits_sumexp[:, :, np.newaxis])

    return ChunkResult(
        per_token_scores=per_token_scores,
        top_token_ids=top_ids,
        top_token_probs=top_probs,
        next_token_logits=next_token_logits,
        kv_cache=self.kv_cache,
    )


class InferFn(typing_extensions.Protocol):
  """A function providing a forwards pass through a model."""

  def __call__(self, weights, kv_caches,
               chunk):
    Ellipsis
