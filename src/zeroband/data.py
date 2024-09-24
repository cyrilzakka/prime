from functools import partial
from typing import Any, Generator

import torch
from torch.utils.data import DataLoader
from torch.utils.data import IterableDataset

from datasets import load_dataset
from datasets.distributed import split_dataset_by_node

TEST_VOCAB_SIZE = 1024

# TODO sami: make sure the init of the model is the same on all rank


class FakeTokenizedDataset(IterableDataset):
    """This is a dummy dataset that generates random sequences of length seq_len and vocab_size"""

    def __init__(self, seq_len: int, vocab_size: int):
        self.seq_len = seq_len
        self.vocab_size = vocab_size
        assert vocab_size > 3, "Vocab size must be greater than 3"

    def __iter__(self) -> Generator[dict[str, Any], Any, None]:
        while True:
            input_ids = torch.randint(3, self.vocab_size, (self.seq_len,)).tolist()
            yield {"input_ids": input_ids}


def collate_causal_mask(max_seq_length: int = -1, pad_id: int = 0, ignore_index: int = -100) -> callable:
    """collate function for causal mask. Fill with padding tokens if sequence is shorter than max_seq_length"""
    return partial(_collate_fn_causal_mask, max_seq_length=max_seq_length, pad_id=pad_id, ignore_index=ignore_index)


def _collate_fn_causal_mask(
    samples: list[dict[str, torch.LongTensor]], max_seq_length: int = -1, pad_id: int = 0, ignore_index: int = -100
) -> dict[str, torch.LongTensor]:
    """collate function for causal mask. Fill with padding tokens if sequence is shorter than max_seq_length.
    input_ids and labels are both of size max_seq_length.
    """

    assert samples[0].keys() == {"input_ids"}

    batched = {"input_ids": [], "labels": []}

    if max_seq_length > 0:
        max_seq_length += 1  # this makes sure that the effective seqlen is correct

    for sample in samples:
        input_ids = torch.Tensor(sample["input_ids"]).long()

        if len(input_ids) < max_seq_length:
            input_ids = torch.cat([input_ids, torch.full((max_seq_length - len(input_ids),), pad_id)])
        elif len(input_ids) > max_seq_length:
            input_ids = input_ids[:max_seq_length]

        batched["input_ids"].append(input_ids[:-1])
        batched["labels"].append(input_ids[1:])

    return {"input_ids": torch.stack(batched["input_ids"], dim=0), "labels": torch.stack(batched["labels"], dim=0)}


def get_dataloader(
    tokenizer, world_size: int, rank: int, seq_length: int, batch_size: int, num_workers: int, fake_data: bool
) -> DataLoader:
    if fake_data:
        train_dataset = FakeTokenizedDataset(seq_length, TEST_VOCAB_SIZE)
    else:
        ds = load_dataset("allenai/c4", "en", streaming=True)

        def tokenize_function(data):
            outputs = tokenizer(data["text"], truncation=True, max_length=seq_length, padding="max_length")
            return outputs

        tokenized_datasets = ds.map(
            tokenize_function, batched=True, remove_columns=["text", "timestamp", "url", "attention_mask"]
        )["train"]
        train_dataset = split_dataset_by_node(tokenized_datasets, world_size=world_size, rank=rank)

    data_collator = collate_causal_mask(max_seq_length=seq_length, pad_id=tokenizer.pad_token_id, ignore_index=-100)

    return DataLoader(
        train_dataset,
        collate_fn=data_collator,
        batch_size=batch_size,
        num_workers=num_workers,
    )
