# Copyright 2024 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
import logging
import os
import random
from typing import Any, Iterable, Optional, Sequence, Union

import torch
import transformers
from composer.core import DataSpec
from composer.core.data_spec import _default_split_batch, _split_list
from composer.utils import MissingConditionalImportError, dist, get_file
from datasets import Dataset as HFDataset
from datasets import IterableDataset, load_dataset
from torch.utils.data import DataLoader, Dataset
from transformers import PreTrainedTokenizerBase

from llmfoundry import registry
from llmfoundry.eval.datasets.utils import (
    convert_tokens_to_tensors,
    get_continuation_span,
    get_fewshot_sample_idxs,
    make_padded_input,
    stop_sequences_criteria,
    strip_data,
    tokenizer_needs_prefix_space,
    trim_context,
)
from llmfoundry.utils.registry_utils import construct_from_registry

log = logging.getLogger(__name__)

# Allow models to have slightly more tokens than were used in the most verbose CoT in the dataset
_MAX_ANSWER_BUFFER_LENGTH = 10

__all__ = [
    'InContextLearningDataset',
    'InContextLearningLMTaskDataset',
    'InContextLearningMultipleChoiceTaskDataset',
    'InContextLearningSchemaTaskDataset',
    'InContextLearningGenerationTaskWithAnswersDataset',
    'get_icl_task_dataloader',
]


class InContextLearningDataset(Dataset):
    r"""A base dataset that constructs batches for in-context learning task.

    evaluations. The dataset format is expected to be a local jsonl file, a
    cloud link to a jsonl file, or a Hugging Face dataset link. 'context' refers
    to the input a model will receive before generating an output. For example,
    the question in question answering tasks, the preceding text in a language
    modeling task, or the document and question regarding the document in a
    document understanding task. 'example' refers to a loaded dictionary,
    generally containing a context, an answer, and any other information needed
    to run the task. 'answer' refers to the desired output of the model.

    When creating a new ICL Dataset, it is likely that you will need to reimplement the following methods:

    - construct_context(): Takes a single example dictionary and formulates the context as a string for that eval question.
    - get_answer_from_example(): Takes a single example dictionary and formulates the correct, ground truth answer as a string.
    - tokenize_example(): Tokenizes the example and adds any extra content from the original dictionary that needs to be passed downstream.
    - read_dataset(): Loads the dataset and does basic parsing. If additional parsing must be done, this is a good place to do so (See InContextLearningGenerationTaskWithAnswersDataset.read_dataset())

    Additionally, base_batch and batch_mapping must be defined.

    - base_batch (Dict): The base dictionary that the dataset will use to construct a batch. This should contain static values, like generation_kwargs or mode,
      and empty lists for values that will need to be accumulated from each example.
      NOTE: Sometimes you will need to set base_batch directly after the init call, e.g. in order to use class variables
      like self.pad_tok_id or self.max_answer_length. If you manually set generation_kwargs this way, you'll need to call self.update_generation_kwargs()
      after setting self.base_batch.
    - batch_mapping (Dict): A mapping with keys that are keys in the batch and values that are columns in the loaded dataset.
      collate_fn will use this mapping to create batches from self.dataset.

    Args:
        dataset_uri (str): A local path, a remote path beginning with ``s3://`` or another backend, or a HuggingFace dataset uri prepended with ``hf://``.
            Alternate backends must be supported by :meth:`composer.utils.maybe_create_object_store_from_uri`.
            A local dataset must consist of rows of JSON data points with task dependent fields.
            The default keys expected are "context" and "answer".
        tokenizer (transformers.PreTrainedTokenizerBase): The tokenizer used to map between strings and token ids.
        max_seq_len (int): The maximum sequence length supported by the model.
        pad_tok_id (int): The special token used for padding batches.
        num_fewshot (int): The number of complete fewshot examples to prepend before each test example. These are not identical across examples.
        fewshot_random_seed (int): Random seed to use for fewshot sampling.
        prompt_string (str): Prompt string to put once before all fewshot examples/test examples (e.g. 'Translate english to french.').
        example_delimiter (str): Separator inserted before (context, answer) pairs (e.g. '\\n') for fewshot sampling and prompting.
        continuation_delimiter: (str): Separator inserted between context and answer in each example (e.g. '\\nA: ').
        destination_path (str): Temporary path to store downloaded datasets.
        prelimiter (str): Text to be prepended before each context, including few shot examples (e.g. "Question: ").
        context_key (str): The key in the loaded dataset that contains the context.
        answer_key (str): The key in the loaded dataset that contains the answer.
        strip_dataset (bool): Boolean for whether to strip whitespace from data. Trailing whitespace can cause degenerative outputs,
            so unless whitespace should be preserved (for example in code), this should be set to True.
        padding_side (str): Side of the content and answer on which to apply padding. Can be either 'right' or 'left'.
        tokenize_labels (bool): Whether or not the labels should be tokenized. Generally determined by which metric a dataset uses.
        padding_size (int): The final size of the tensor after padding. Defaults to max_sequence_length.
        base_batch (Dict): The base dictionary upon which a batch is created. See above for more details.
        base_mapping (Dict): A mapping of batch keys to dataset columns, used to create batches. See above for more details.
        hf_loading_vars (Dict): A dictionary containing keyword arguments to be passed into `load_dataset` if dataset is being pulled from HF.
        hf_parsing_map (Dict): A dictionary containing a mapping from HF columns to ICL dataset keys. The dictionary should be formatted {icl_key:[hf_key1, hf_key1]}.
            Column contents will be concatenated with ' ' separating them. If not included, will load the columns already present in the HF dataset.
        generation_kwargs (Dict): A dictionary containing keyword arguments to be passed along to the model's generate function.
        static_keys (List): A list of the key values which will be broadcast across a batch (e.g. it is the same for each batch element).
        list_keys (List): A list of the batch keys whose values are lists which will be split using list methods during calls to split_batch.
        tensor_keys (List): A list of the batch keys whose values are tensors which will be split using tensor methods during calls to split_batch.
    """

    def __init__(
        self,
        dataset_uri: str,
        tokenizer: transformers.PreTrainedTokenizerBase,
        max_seq_len: int,
        pad_tok_id: int,
        num_fewshot: int,
        destination_path: str,
        fewshot_random_seed: int = 1234,
        prompt_string: str = '',
        example_delimiter: str = '\n',
        continuation_delimiter: str = ' ',
        prelimiter: str = '',
        context_key: str = 'context',
        answer_key: str = 'answer',
        strip_dataset: bool = True,
        padding_side: str = 'right',
        tokenize_labels: bool = True,
        padding_size: Optional[int] = None,
        base_batch: Optional[dict] = None,
        batch_mapping: Optional[dict] = None,
        hf_loading_vars: Optional[dict] = None,
        hf_parsing_map: Optional[dict] = None,
        generation_kwargs: Optional[dict] = None,
        static_keys: Optional[list] = None,
        list_keys: Optional[list] = None,
        tensor_keys: Optional[list] = None,
    ):
        self.tokenizer = tokenizer
        self.prefix_space = tokenizer_needs_prefix_space(self.tokenizer)

        self.max_seq_len = max_seq_len
        self.pad_tok_id = pad_tok_id
        self.num_fewshot = num_fewshot
        self.padding_side = padding_side
        self.padding_size = padding_size if padding_size else self.max_seq_len
        self.prelimiter = prelimiter
        self.example_delimiter = example_delimiter
        self.continuation_delimiter = continuation_delimiter
        self.context_key = context_key
        self.answer_key = answer_key
        self.tokenize_labels = tokenize_labels
        self.batch_mapping = batch_mapping or {}
        self.base_batch = base_batch or {}
        if generation_kwargs:
            self.update_generation_kwargs(generation_kwargs)

        self.static_keys = static_keys
        self.list_keys = list_keys
        self.tensor_keys = tensor_keys

        hf_loading_vars = hf_loading_vars or {}
        self.dataset: HFDataset = self.read_dataset(
            dataset_uri,
            destination_path,
            hf_loading_vars,
            hf_parsing_map,
        )
        self.strip_data = strip_dataset
        if self.strip_data:
            self.dataset = self.dataset.map(strip_data)

        fewshot_rng = random.Random(fewshot_random_seed)
        self._prepared = False
        self.num_fewshot = num_fewshot
        self.prompt_string = prompt_string
        self.fewshot_rng = fewshot_rng

    def _prepare_dataset(self):
        self.dataset: HFDataset = self.dataset.map(
            self._prep_example,
            with_indices=True,
            fn_kwargs={
                'num_fewshot': self.num_fewshot,
                'prompt_string': self.prompt_string,
                'fewshot_rng': self.fewshot_rng,
            },
        )
        self._prepared = True

    def __getitem__(self, index: int) -> dict:
        if not self._prepared:
            self._prepare_dataset()
        return self.dataset[index]

    def __len__(self) -> int:
        return len(self.dataset)

    def get_num_samples_in_batch(self, batch: dict) -> int:
        return batch['input_ids'].shape[0]

    def get_effective_batch_size(self, batch_size: int) -> int:
        r"""Returns effective batch size computed for given ICL task.

        The effective batch size may not be equal to the configured evaluation
        batch size because for certain ICL tasks, >1 prompts can get created
        for every input query depending on the number of choices/continuations.
        This requires the effective batch size to be reduced to prevent larger batches than expected during eval. For example,
        check InContextLearningMultipleChoiceTaskDataset.

        Args:
            batch_size (int): Original batch size configured for ICL evaluations
        """
        return batch_size

    def update_generation_kwargs(self, generation_kwargs: dict) -> None:
        r"""Updates self.base_batch with the passed in generation_kwargs.

        This must be run after self.base_batch is set (for example, if
        self.base_batch is set after __init__() is run, likely because
        base_batch needs a class variable like self.pad_tok_id or
        self.max_answer_length).

        Args:
            generation_kwargs (Dict): Keyword arguments that be written into base_batch['generation_kwargs']
        """
        if generation_kwargs:
            if 'generation_kwargs' not in self.base_batch:
                self.base_batch['generation_kwargs'] = {}
            self.base_batch['generation_kwargs'].update(generation_kwargs)

    def read_dataset(
        self,
        dataset_uri: str,
        destination_path: str,
        hf_loading_vars: Optional[dict[str, Any]] = None,
        hf_parsing_map: Optional[dict[str, Any]] = None,
    ) -> 'HFDataset':
        """Reads a dataset and handles parsing it from HuggingFace.

        Args:
            dataset_uri (str): A local path, a remote path beginning with ``s3://`` or another backend, or a HuggingFace dataset uri.
                Alternate backends must be supported by :meth:`composer.utils.maybe_create_object_store_from_uri`.
            destination_path (str): A local path where the data will be stored
            hf_loading_vars (Dict): If parsing from HuggingFace, keyword args that will be passed into load_dataset
            hf_parsing_map (Dict): Dictionary in the form of {icl_key: [hf_col1, hf_col2]} that will map one or more hf columns, in order, to ICL dataset columns

        Returns:
            dataset: A loaded HF dataset
        """
        from datasets import \
            Dataset as HFDataset  # pyright: ignore[reportGeneralTypeIssues]
        from datasets import (  # pyright: ignore[reportGeneralTypeIssues]
            load_dataset,
        )
        if 'hf://' in dataset_uri:
            dataset_uri = dataset_uri.replace('hf://', '')
            if hf_loading_vars is None:
                hf_loading_vars = {}
            dataset = load_dataset(dataset_uri, **hf_loading_vars)
            if hf_parsing_map:
                dataset_parsing_func = lambda example: {
                    k: ' '.join([str(example[col]) for col in v])
                    for k, v in hf_parsing_map.
                    items(  # pyright: ignore[reportOptionalMemberAccess]
                    )
                }
                assert isinstance(dataset, HFDataset)
                dataset = dataset.map(
                    dataset_parsing_func,
                    remove_columns=dataset.column_names,
                )
        else:
            with dist.local_rank_zero_download_and_wait(destination_path):
                if dist.get_local_rank() == 0:
                    get_file(dataset_uri, destination_path, overwrite=True)
            dataset = load_dataset(
                'json',
                data_files=destination_path,
                split='train',
                streaming=False,
            )
        assert isinstance(dataset, HFDataset)
        return dataset

    def _generate_few_shot_prompt(
        self,
        num_fewshot: int,
        example_idx: int,
        preamble: str,
        fewshot_rng: random.Random,
    ) -> str:
        """Formats the fewshot prompt for test example `example_idx`.

        Randomly selects `num_fewshot` samples from the dataset (excluding the example at `example_idx`) and constructs
        contexts with answers appended.

        Returns the formatted prompt_string + concatenated list of formatted few shot examples as a string.

        Args:
            num_fewshot (int): Number of examples to prepend
            example_idx (int): Current example idx
            preamble (str): Text to occur at the beginning of the task. Generally instructions or a prompt.
            fewshot_rng (random.Random): Seeded sampler to chose samples with

        Returns:
            str: The original preamble with num_fewshot examples appended
        """
        few_shot_text = preamble

        if num_fewshot > 0:
            fewshot_idxs = get_fewshot_sample_idxs(
                len(self.dataset),
                num_fewshot,
                example_idx,
                fewshot_rng,
            )
            for fewshot_idx in fewshot_idxs:
                ctxt = self.construct_context(
                    self.dataset[fewshot_idx],
                    few_shot_text,
                    add_answer=True,
                )
                few_shot_text += ctxt

        return few_shot_text

    def construct_context(
        self,
        example: dict,
        preceding_text: str = '',
        add_answer: bool = False,
    ) -> str:
        """Takes an example and constructs a context, i.e. the input the model.

        reads for this example. Optionally adds the correct answer (for fewshot
        examples) and handles example delimiters.

        Args:
            example (Dict): The example from which to construct the context
            preceding_text (str): Any preceding text, used as a check for prepending self.example_delimiter
            add_answer (bool): Bool for whether or not to add the answer on the end of the context (e.g. for fewshot examples)

        Returns:
            str: The constructed context. The default output context is
                 formatted as follows: f'{self.prelimiter}{example[self.context_key]}{self.continuation_delimiter}'
        """
        ctxt = example[self.context_key]
        ctxt = f'{self.prelimiter}{ctxt}'
        if len(preceding_text) > 0:
            ctxt = f'{self.example_delimiter}{ctxt}'
        ctxt = f'{ctxt}{self.continuation_delimiter}'
        if add_answer:
            ctxt = f'{ctxt}{self.get_answer_from_example(example, in_context=add_answer)}'
        return ctxt

    def get_answer_from_example(
        self,
        example: dict[str, Any],
        in_context: bool = False,
    ) -> str:
        """Returns the answer from the example.

        Args:
            example (Dict): The example from which to retrieve the answer
            in_context (bool): Whether this is an in-context example. Default to False.

        Returns:
            str: The answer in the example
        """
        cont = example[self.answer_key]
        if self.prefix_space and not cont.startswith(' ') and not in_context:
            cont = f' {cont}'
        return cont

    def _fix_eos_on_preamble(self, input_ids: list[int]) -> list[int]:
        """If the input_ids is empty then input_ids will be a 0-length List.

        unless the tokenizer adds special tokens to empty strings (e.g. OPT
        tokenizer). If there is an EOS token added, we need to remove it so it
        is not in the middle of the prompt, as the specific eval question's
        prompt will follow the input_ids.

        Args:
            input_ids (List): The tokenized input

        Returns:
            input_ids: The tokenized input conditionally edited
        """
        if (
            self.tokenizer.eos_token_id is not None and len(input_ids) > 1 and
            input_ids[-1] == self.tokenizer.eos_token_id
        ):
            input_ids = input_ids[:-1]
        return input_ids

    def tokenize_example(
        self,
        prompt_and_fewshot: str,
        ctxt: str,
        example: dict,
    ) -> dict[str, Any]:
        """Runs text through the tokenizer and handle special cases.

        Args:
            prompt_and_fewshot (str): The collection of the prompt and fewshot examples that belongs before the example's context
            ctxt (str): The specific example's derived context
            example (Dict): The example as a dictionary. Used for additional processing in inherited classes.

        Returns:
            Dict: Dictionary with the tokenized data
        """
        tokenized_example = {}
        # Always add special tokens to preamble
        preamble = self.tokenizer(prompt_and_fewshot)['input_ids']
        assert isinstance(preamble, list)
        preamble = self._fix_eos_on_preamble(preamble)
        if self.strip_data:
            # rstrip context because a prompt ending in a space results in degenerate output
            ctxt = ctxt.rstrip()
        # Never add special tokens to context
        tokenized_context = self.tokenizer(
            ctxt,
            add_special_tokens=False,
        )['input_ids']
        assert isinstance(preamble, list)
        assert isinstance(tokenized_context, list)

        tokenized_context = preamble + tokenized_context

        if self.tokenize_labels:
            # Never add special tokens to answer
            tokenized_answer = self.tokenizer(
                self.get_answer_from_example(example),
                add_special_tokens=False,
            )['input_ids']
            assert isinstance(tokenized_answer, list)
            trimmed_context = trim_context(
                tokenized_context,
                tokenized_answer,
                self.padding_size,
            )
            assert isinstance(trimmed_context, list)
            continuation_indices = get_continuation_span(
                trimmed_context,
                tokenized_answer,
            )
            padded_context = make_padded_input(
                trimmed_context,
                tokenized_answer,
                self.padding_size,
                self.pad_tok_id,
                self.padding_side,
            )

            tokenized_example[self.context_key] = padded_context
            tokenized_example[self.answer_key] = tokenized_answer
            tokenized_example['continuation_indices'] = continuation_indices
        else:
            assert isinstance(tokenized_context, list)
            trimmed_context = trim_context(
                tokenized_context,
                [],
                self.padding_size,
            )
            assert isinstance(trimmed_context, list)
            padded_context = make_padded_input(
                trimmed_context,
                [],
                self.padding_size,
                self.pad_tok_id,
                self.padding_side,
            )

            tokenized_example[self.context_key] = padded_context
            tokenized_example[self.answer_key
                             ] = self.get_answer_from_example(example)

        return tokenized_example

    def _prep_example(
        self,
        example: dict,
        example_idx: int,
        num_fewshot: int,
        prompt_string: str,
        fewshot_rng: random.Random,
    ) -> dict[str, Any]:
        """Prepares a single example from a HF Dataset into tokenized format.

        with prompt and fewshot examples.

        Each task consists of a context and a continuation as well as an optional prompt and optional list of
        example context/continuation pairs which precede the test context/continuation pair.

        Args:
            example (Dict): A Dictionary from the hf dataset
            example_idx (int): The index of example
            num_fewshot (int): Number of examples context/continuation pairs to prepend to the test pair
            prompt_string (str): The prompt to prepend to all inputs
            fewshot_rng (random.Random): Random number generator to use for fewshot sampling

        Returns:
            Dict: Contains a dictionary with the tokenized data
        """
        prompt_and_fewshot = self._generate_few_shot_prompt(
            num_fewshot,
            example_idx,
            prompt_string,
            fewshot_rng,
        )
        ctxt = self.construct_context(
            example,
            prompt_and_fewshot,
            add_answer=False,
        )
        tokenized_example = self.tokenize_example(
            prompt_and_fewshot,
            ctxt,
            example,
        )
        return tokenized_example

    def collate_fn(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """The function that the dataloader uses to accumulate data into.

        batches.

        Args:
            data (List): List of tokenized datapoints (dicts returned by self._tokenize_example)

        Returns:
            Dict: Dictionary for a single batch
        """
        batch = copy.deepcopy(self.base_batch)
        for data_pair in data:
            for batch_key, data_key in self.batch_mapping.items():
                batch[batch_key].append(data_pair[data_key])
            if 'continuation_indices' in data_pair:
                batch['continuation_indices'].append(
                    data_pair['continuation_indices'],
                )

        batch = convert_tokens_to_tensors(batch, self.tokenize_labels)
        batch['attention_mask'] = ~(batch['input_ids'] == self.pad_tok_id)
        return batch

    def split_batch(
        self,
        batch: Any,
        microbatch_size: Union[int, float],
    ) -> Sequence[Any]:
        return _default_split_batch(batch, microbatch_size)


class InContextLearningGenerationTaskWithAnswersDataset(
    InContextLearningDataset,
):
    """A dataset that constructs batches for in-context learning generation.

    tasks with answers. Generation tasks evaluate a model's ability to
    generate responses and score them against a set of gold-standard answers.

    The input format is expected to be a jsonl file with the following fields:
    - context: The question
    - answer: The preferred answer to the question
    - aliases: A list of aliases for the answer

    See InContextLearningDataset for more details.

    Additional Args:
        cot_delimiter (str): Delimiter to place between the chain of thought and continuations.
        early_stopping_criteria (Optional[List[str]]): Optional strings to trigger early stopping.
        do_normalization (bool): Flag indicating whether to normalize generations before providing output.
    """

    def __init__(
        self,
        dataset_uri: str,
        tokenizer: transformers.PreTrainedTokenizerBase,
        max_seq_len: int,
        pad_tok_id: int,
        num_fewshot: int,
        destination_path: str,
        fewshot_random_seed: int = 1234,
        prompt_string: str = '',
        example_delimiter: str = '\n',
        continuation_delimiter: str = ' ',
        prelimiter: str = '',
        context_key: str = 'context',
        answer_key: str = 'answer',
        strip_dataset: bool = True,
        padding_size: Optional[int] = None,
        base_batch: Optional[dict] = None,
        batch_mapping: Optional[dict] = None,
        hf_loading_vars: Optional[dict] = None,
        hf_parsing_map: Optional[dict] = None,
        generation_kwargs: Optional[dict] = None,
        cot_delimiter: str = '',
        early_stopping_criteria: Optional[list[str]] = None,
        do_normalization: bool = True,
    ):
        if tokenizer.eos_token_id is None:
            raise ValueError(
                '`InContextLearningGenerationTaskWithAnswersDataset` tokenizer must have non-null `eos_token_id`',
            )
        self.cot_delimiter = cot_delimiter
        self.has_cot = False
        self.max_answer_length = 0
        static_keys = [
            'mode',
            'cot_delimiter',
            'generation_kwargs',
            'do_normalization',
            'stopping_criteria',
        ]
        tensor_keys = ['input_ids', 'attention_mask']
        list_keys = ['labels']
        super().__init__(
            dataset_uri=dataset_uri,
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            pad_tok_id=pad_tok_id,
            num_fewshot=num_fewshot,
            fewshot_random_seed=fewshot_random_seed,
            prompt_string=prompt_string,
            example_delimiter=example_delimiter,
            continuation_delimiter=continuation_delimiter,
            destination_path=destination_path,
            prelimiter=prelimiter,
            context_key=context_key,
            answer_key=answer_key,
            strip_dataset=strip_dataset,
            padding_size=padding_size,
            base_batch=base_batch,
            batch_mapping=batch_mapping,
            hf_loading_vars=hf_loading_vars,
            hf_parsing_map=hf_parsing_map,
            generation_kwargs=generation_kwargs,
            # specific to ICL dataset
            padding_side='left',
            tokenize_labels=False,
            static_keys=static_keys,
            list_keys=list_keys,
            tensor_keys=tensor_keys,
        )
        # NOTE: set these after init call because they take class vars
        self.early_stopping_criteria = early_stopping_criteria
        self.base_batch = {
            'input_ids': [],
            'mode': 'generate',
            'labels': [],
            'cot_delimiter': self.cot_delimiter,
            'stopping_criteria': early_stopping_criteria,
            'do_normalization': do_normalization,
            'generation_kwargs': {
                'pad_token_id': self.pad_tok_id,
                'use_cache': True,
                'eos_token_id': self.tokenizer.eos_token_id,
                'max_new_tokens': max(self.max_answer_length, 1),
            },
        }
        self.batch_mapping = {
            'input_ids': self.context_key,
            'labels': 'aliases',
        }
        if generation_kwargs:
            self.update_generation_kwargs(generation_kwargs)

    def read_dataset(
        self,
        dataset_uri: str,
        destination_path: str,
        hf_loading_vars: dict,
        hf_parsing_map: dict,
    ) -> 'HFDataset':
        dataset = super().read_dataset(
            dataset_uri,
            destination_path,
            hf_loading_vars,
            hf_parsing_map,
        )
        self.has_cot = 'chain_of_thought' in dataset.features
        dataset = dataset.map(
            lambda examples: {
                'context':
                    examples['context'],
                'answer':
                    examples['answer'],
                'aliases':
                    set([examples['answer']] + examples.get('aliases', [])),
                'chain_of_thought':
                    examples.get('chain_of_thought', ''),
            },
        )
        self.max_answer_length = self._get_max_answer_length(dataset)
        # NOTE: This is the only time we use the class variable padding_size.
        if self.max_seq_len < self.max_answer_length:
            log.warning(f'`max_seq_len` {self.max_seq_len} was less than `max_answer_len`: {self.max_answer_length}' \
                        + ' setting  `max_seq_len`=`max_answer_len`')
            self.max_seq_len = self.max_answer_length
        self.padding_size = self.max_seq_len - self.max_answer_length
        return dataset

    def get_answer_from_example(
        self,
        example: dict,
        in_context: bool = False,
    ) -> str:
        """Returns the answer from the example. Applies chain of thought if.

        self.has_cot is marked as true.

        Args:
            example (Dict): The example from which to retrieve the answer
            in_context (bool): Whether this is an in-context example. Default to False.

        Returns:
            str: The answer in from the example with chain of thought and delimiter if needed
        """
        if self.has_cot:
            example_cot = example['chain_of_thought']
            return f'{example_cot}{self.cot_delimiter}{example[self.answer_key]}'
        else:
            return example[self.answer_key]

    def tokenize_example(
        self,
        prompt_and_fewshot: str,
        ctxt: str,
        example: dict,
    ) -> dict[str, Any]:
        """Run text through the tokenizer and handle special cases.

        Args:
            prompt_and_fewshot (str): The collection of the prompt and fewshot examples that belongs before the example's context
            ctxt (str): The specific example's derived context
            example (Dict): The example as a dictionary.

        Returns:
            Dict: Dictionary with the tokenized data
        """
        tokenized_example = super().tokenize_example(
            prompt_and_fewshot,
            ctxt,
            example,
        )
        tokenized_example['aliases'] = list(example.get('aliases', []))
        return tokenized_example

    def _get_max_answer_length(self, dataset: Iterable[dict]) -> int:
        """Loops over the dataset and finds the longest answer length.

        Returns:
            int: The maximum answer length with an additional buffer of 10 if chain of thought is present
        """
        max_answer_length = 0
        for example in dataset:
            all_answers = [
                example[self.answer_key],
            ] + list(example.get('aliases', []))
            for answer in all_answers:
                if self.has_cot:
                    example_cot = example['chain_of_thought']
                    response = (f'{example_cot}{self.cot_delimiter}{answer}')
                else:
                    response = answer
                tokenized_response = self.tokenizer(response)['input_ids']
                assert isinstance(tokenized_response, list)
                max_answer_length = max(
                    max_answer_length,
                    len(tokenized_response),
                )
        max_answer_length = max_answer_length + (
            _MAX_ANSWER_BUFFER_LENGTH if len(self.cot_delimiter) > 0 else 0
        )
        return max_answer_length

    def collate_fn(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        batch = super().collate_fn(data)
        batch_size = batch['input_ids'].shape[0]
        stopping_criteria = None
        if self.early_stopping_criteria:
            if stop_sequences_criteria is None:  # pyright: ignore [reportUnnecessaryComparison]
                raise MissingConditionalImportError(
                    extra_deps_group='nlp',
                    conda_package='transformers',
                    conda_channel='conda-forge',
                )
            stopping_criteria = stop_sequences_criteria(
                self.tokenizer,
                self.early_stopping_criteria,
                batch_size,
            )
        batch['generation_kwargs']['stopping_criteria'] = stopping_criteria
        return batch

    def split_batch(self, batch: Any,
                    microbatch_size: Union[int, float]) -> Sequence[Any]:
        """Split batch handling for special columns.

        Args:
            batch (Dict): Batch of data
            microbatch_size (int | float): Size of microbatches

        Returns:
            List: List of chunked batches
        """
        # Don't split kwargs that don't change
        # Normally split torch tensors
        # List split lists of strings
        if isinstance(microbatch_size, float):
            raise ValueError(
                'split_batch does not support floating point microbatch_size.',
            )
        chunked = {}
        for k, v in batch.items():
            if k in self.static_keys:
                # Defer broadcasting until we know num_chunks
                pass
            elif k in self.list_keys:
                chunked[k] = _split_list(v, microbatch_size)
            elif k in self.tensor_keys:
                chunked[k] = _default_split_batch(v, microbatch_size)
            else:
                raise ValueError(f'Unexpected key {k} in batch splitting')
        num_chunks = len(chunked['input_ids'])
        for k, v in batch.items():
            if k in self.static_keys:
                chunked[k] = [v] * num_chunks

        batched_list = [{
            k: v[idx] for k, v in chunked.items()
        } for idx in range(num_chunks)]
        return batched_list


class InContextLearningLMTaskDataset(InContextLearningDataset):
    """A dataset that constructs batches for in-context learning language.

    modeling evaluation. Language modeling tasks test a model's ability to
    properly predict tokens based on preceding tokens.

    The input format is expected to be a jsonl file with the following fields:
    - context: Preceding text
    - continuation: The expected continuation

    See InContextLearningDataset for more details.
    """

    def __init__(
        self,
        dataset_uri: str,
        tokenizer: transformers.PreTrainedTokenizerBase,
        max_seq_len: int,
        pad_tok_id: int,
        num_fewshot: int,
        destination_path: str,
        fewshot_random_seed: int = 1234,
        prompt_string: str = '',
        example_delimiter: str = '\n',
        continuation_delimiter: str = ' ',
        prelimiter: str = '',
        context_key: str = 'context',
        strip_dataset: bool = True,
        tokenize_labels: bool = True,
        padding_size: Optional[int] = None,
        hf_loading_vars: Optional[dict] = None,
        hf_parsing_map: Optional[dict] = None,
        generation_kwargs: Optional[dict] = None,
        static_keys: Optional[list] = None,
        list_keys: Optional[list] = None,
    ):
        super().__init__(
            dataset_uri=dataset_uri,
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            pad_tok_id=pad_tok_id,
            num_fewshot=num_fewshot,
            fewshot_random_seed=fewshot_random_seed,
            prompt_string=prompt_string,
            example_delimiter=example_delimiter,
            continuation_delimiter=continuation_delimiter,
            destination_path=destination_path,
            prelimiter=prelimiter,
            context_key=context_key,
            strip_dataset=strip_dataset,
            tokenize_labels=tokenize_labels,
            padding_size=padding_size,
            hf_loading_vars=hf_loading_vars,
            hf_parsing_map=hf_parsing_map,
            generation_kwargs=generation_kwargs,
            list_keys=list_keys,
            # specific to ICL dataset
            answer_key='continuation',
            static_keys=['mode'],
            tensor_keys=[
                'input_ids',
                'continuation_indices',
                'labels',
                'attention_mask',
            ],
            base_batch={
                'input_ids': [],
                'continuation_indices': [],
                'mode': 'icl_task',
                'labels': [],
            },
            batch_mapping={
                'input_ids': 'context',
                'labels': 'context',
            },
            padding_side='right',
        )


class InContextLearningMultipleChoiceTaskDataset(InContextLearningDataset):
    """A dataset that construct batches for in-context learning multiple choice.

    evaluation.

    If each question has N answer choices, we construct N distinct inputs per question. In order to ensure
    consistency across multi-GPU, we set the batch size to be `min(N, batch_size)` so that all N
    inputs per question can stored in the same batch.

    The default input format is a jsonl file with the following fields:
    - query: The preceding text, question, or document relevant to the choices
    - gold: Index of the correct choice under 'choices'
    - choices: A list of strings, each being one of the potential choices

    Each batch then consists of ``|batch_size // N|`` distinct questions and has the following the structure.
    - input_ids: Input tensor ``|batch x seqlen x # tokens|``
    - continuation_indices: List of ``|batch|`` consisting of tensors indicating which indices in the sequence correspond to the question answer (aka continuation)
    - mode: Indicates to the model that this is an ICL task and may rely on a custom code path to properly update metrics
    - labels: Identical to the input, used by the model to calculate loss/metrics
    - gold_indices: List of length ``|batch_size // N|`` indicating for each question, which of the answers is correct (via an integer [0, N-1])
    - choice_groupings: Indicates which indices of the batch correspond to which questions

    Additional Args:
        choices_key (str): The key under which the choices are stored in the saved dataset. Defaults to 'choices'.
    """

    def __init__(
        self,
        dataset_uri: str,
        tokenizer: transformers.PreTrainedTokenizerBase,
        max_seq_len: int,
        pad_tok_id: int,
        num_fewshot: int,
        destination_path: str,
        fewshot_random_seed: int = 1234,
        prompt_string: str = '',
        example_delimiter: str = '\n',
        continuation_delimiter: str = ' ',
        prelimiter: str = '',
        context_key: str = 'query',
        tensor_keys: Optional[list] = None,
        answer_key: str = 'answer',
        strip_dataset: bool = True,
        tokenize_labels: bool = True,
        padding_size: Optional[int] = None,
        batch_mapping: Optional[dict] = None,
        hf_loading_vars: Optional[dict] = None,
        hf_parsing_map: Optional[dict] = None,
        generation_kwargs: Optional[dict] = None,
        list_keys: Optional[list] = None,
        choices_key: str = 'choices',
        static_keys: Optional[list] = None,
        list_of_tensors_keys: Optional[list] = None,
        list_of_tuples_keys: Optional[list] = None,
        list_of_primitives: Optional[list] = None,
    ):
        self.choices_key = choices_key
        base_batch = {
            'input_ids': [],
            'continuation_indices': [],
            'mode': 'icl_task',
            'labels': [],
            'gold_indices': [],
            'choice_groupings': [],
        }
        if not static_keys:
            static_keys = ['mode', 'generation_kwargs']
        if not tensor_keys:
            tensor_keys = ['input_ids', 'labels', 'attention_mask']
        self.list_of_tensors_keys = list_of_tensors_keys or [
            'continuation_indices',
        ]
        self.list_of_tuples_keys = list_of_tuples_keys or ['choice_groupings']
        self.list_of_primitives = list_of_primitives or ['gold_indices']
        super().__init__(
            dataset_uri=dataset_uri,
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            pad_tok_id=pad_tok_id,
            num_fewshot=num_fewshot,
            fewshot_random_seed=fewshot_random_seed,
            prompt_string=prompt_string,
            example_delimiter=example_delimiter,
            continuation_delimiter=continuation_delimiter,
            destination_path=destination_path,
            prelimiter=prelimiter,
            answer_key=answer_key,
            strip_dataset=strip_dataset,
            tokenize_labels=tokenize_labels,
            padding_size=padding_size,
            batch_mapping=batch_mapping,
            hf_loading_vars=hf_loading_vars,
            hf_parsing_map=hf_parsing_map,
            generation_kwargs=generation_kwargs,
            list_keys=list_keys,
            # specific to ICL dataset
            context_key=context_key,
            base_batch=base_batch,
            static_keys=static_keys,
            tensor_keys=tensor_keys,
            padding_side='right',
        )
        self.num_choices = len(self.dataset[0][self.choices_key])
        self.batch_mapping_per_choice = {
            'input_ids': 'context',
            'labels': 'context',
        }
        self.batch_map_per_example = {'gold_indices': 'gold'}

    def get_effective_batch_size(self, batch_size: int) -> int:
        batch_size = max(self.num_choices, batch_size)
        effective_batchsize = batch_size // self.num_choices
        return effective_batchsize

    def get_answer_from_example(
        self,
        example: dict,
        in_context: bool = False,
    ) -> str:
        """Returns the correct answer from the example's choices.

        Args:
            example (Dict): The example from which to retrieve the answer
            in_context (bool): Whether this is an in-context example. Default to False.

        Returns:
            str: The full string of the correct answer based on the 'gold' key
        """
        choices = example[self.choices_key]
        gold_idx = example['gold']
        return choices[gold_idx]

    def tokenize_example(
        self,
        prompt_and_fewshot: str,
        ctxt: str,
        example: dict,
    ) -> dict[str, Any]:
        """Runs text through the tokenizer and handle special cases.

        Args:
            prompt_and_fewshot (str): The collection of the prompt and fewshot examples that belongs before the example's context
            ctxt (str): The specific example's derived context
            example (Dict): The example as a dictionary.

        Returns:
            Dict: Dictionary with the tokenized data
        """
        # NOTE: some of this is repeated from super class but for loop makes things considerably different
        tokenized_example = {}
        # Always add special tokens to preamble
        preamble = self.tokenizer(prompt_and_fewshot)['input_ids']
        assert isinstance(preamble, list)
        preamble = self._fix_eos_on_preamble(preamble)
        if self.strip_data:
            # rstrip context because a prompt ending in a space results in degenerate output
            ctxt = ctxt.rstrip()
        # Never add special tokens to context
        tokenized_context = self.tokenizer(
            ctxt,
            add_special_tokens=False,
        )['input_ids']
        assert isinstance(tokenized_context, list)
        tokenized_context = preamble + tokenized_context

        tokenized_example[self.context_key] = []
        tokenized_example[self.answer_key] = []
        tokenized_example['continuation_indices'] = []
        # NOTE: Treating tokenize_labels as True for all MC datasets (required for our MC accuracy metric)
        for choice in example[self.choices_key]:
            if self.prefix_space:
                choice = f' {choice}' if not choice.startswith(' ') else choice

            # Never add special tokens to answer
            tokenized_answer = self.tokenizer(
                choice,
                add_special_tokens=False,
            )['input_ids']
            assert isinstance(tokenized_context, list)
            assert isinstance(tokenized_answer, list)
            trimmed_context = trim_context(
                tokenized_context,
                tokenized_answer,
                self.padding_size,
            )
            assert isinstance(trimmed_context, list)
            continuation_indices = get_continuation_span(
                trimmed_context,
                tokenized_answer,
            )
            padded_context = make_padded_input(
                trimmed_context,
                tokenized_answer,
                self.padding_size,
                self.pad_tok_id,
                self.padding_side,
            )

            tokenized_example[self.context_key].append(padded_context)
            tokenized_example[self.answer_key].append(tokenized_answer)
            tokenized_example['continuation_indices'].append(
                continuation_indices,
            )

        tokenized_example['gold'] = example['gold']
        return tokenized_example

    def collate_fn(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """The function that the dataloader uses to accumulate data into.

        batches. We run each distinct query + answer choice through the model
        separately and determine which answer has the lowest per-token-
        perplexity.

        If each question has N possible choices, all N must be grouped together as distinct elements of the batch
        since the batch may consist of multiple questions, the choice_groupings indicates
        which contiguous sequences of elements in the batch correspond to which question
        gold_indices indicates which of the [0, N-1] choices is the correct one for each question.

        Args:
            data (List): List of tokenized datapoints (dicts returned by self._tokenize_example)

        Returns:
            Dict: Dictionary for a single batch
        """
        batch = copy.deepcopy(self.base_batch)
        for data_pair in data:
            choice_start_idx = len(batch['continuation_indices'])
            # NOTE: not using batch_mapping
            for i, context_enc in enumerate(data_pair[self.context_key]):
                batch['input_ids'].append(context_enc)
                batch['continuation_indices'].append(
                    data_pair['continuation_indices'][i],
                )
                batch['labels'].append(context_enc)

            batch['gold_indices'].append(data_pair['gold'])
            choice_end_idx = len(batch['continuation_indices'])
            batch['choice_groupings'].append((choice_start_idx, choice_end_idx))

        batch = convert_tokens_to_tensors(batch, self.tokenize_labels)
        batch['attention_mask'] = ~(batch['input_ids'] == self.pad_tok_id)
        return batch

    def get_num_samples_in_batch(self, batch: dict[str, torch.Tensor]) -> int:
        return batch['input_ids'].shape[0] // self.num_choices

    def split_batch(self, batch: Any,
                    microbatch_size: Union[int, float]) -> Sequence[Any]:
        """Split batch while ensuring all continuations are in the same.

        microbatch.

        In ICL Multiple Choice, we duplicate each data point for each possible continuation.
        When splitting a batch, we have logical example, which refer to one possible question,
        and real example, which refers to one possible continuation. As example count and
        microbatch_size are tracked in logical example, we split logical attributes by
        microbatch_size and real attributes by microbatch_size * num_choices.

        Args:
            batch (Dict): Batch of data
            microbatch_size (int | float): Size of microbatches

        Returns:
            list: List of chunked batches
        """
        if isinstance(microbatch_size, float):
            raise ValueError(
                'split_batch does not support floating point microbatch_size.',
            )
        chunked = {}
        for k, v in batch.items():
            if k in self.static_keys:
                # Defer broadcasting primitives until we know num_chunks
                pass
            elif type(v) == list:
                # list of tensors - 'continuation_indices'
                if k in self.list_of_tensors_keys:
                    chunked[k] = _split_list(
                        v,
                        microbatch_size * self.num_choices,
                    )
                # list of tuples - 'choice_groupings'
                elif k in self.list_of_tuples_keys:
                    chunked[k] = _split_list(v, microbatch_size)
                # list - 'gold_indices'
                elif k in self.list_of_primitives:
                    chunked[k] = _default_split_batch(v, microbatch_size)
                else:
                    raise ValueError(f'Unexpected key {k} in list splitting')
            elif k in self.tensor_keys:
                chunked[k] = _default_split_batch(
                    v,
                    microbatch_size * self.num_choices,
                )
            else:
                raise ValueError(f'Unexpected key {k} in batch splitting')
        num_chunks = len(chunked['input_ids'])
        # Broadcast primitives to all chunks
        for k, v in batch.items():
            if k in self.static_keys:
                chunked[k] = [v] * num_chunks

        return [{
            k: v[idx] for k, v in chunked.items()
        } for idx in range(num_chunks)]


class InContextLearningSchemaTaskDataset(
    InContextLearningMultipleChoiceTaskDataset,
):
    """A dataset that constructs batches for in-context learning schema.

    evaluation. A schema task involves sentences with a fill-in-the-blank where
    the user needs to choose the correct word to fill in from a set of N
    options. We use the partial evaluation technique from
    https://arxiv.org/abs/1806.02847 to determine the model's choice of fill-in
    word.

    The default input format is a jsonl file with the following fields:
    - context_options: List of strings corresponding to possible preceding context options for the continuation
    - gold: Index of the correct context from 'context_options'
    - continuation: The finishing continuation

    Each batch then consists of ``batch_size // N`` distinct tasks and has the following the structure
    - input_ids: Input tensor ``batch x seqlen x # of tokens``
    - continuation_indices: List of ``batch`` consisting of tensors indicating which indices in the sequence correspond to the question answer (aka continuation)
    - mode: Indicates to the model that this is an ICL task and may rely on a custom code path to properly update metrics
    - labels: Identical to the input, used by the model to calculate loss/metrics
    - gold_indices: List of length ``batch_size // N`` indicating for each question, which of the answers is correct (via an integer [0, N-1])
    - choice_groupings: Indicates which indices of the batch correspond to which questions
    """

    def __init__(
        self,
        dataset_uri: str,
        tokenizer: transformers.PreTrainedTokenizerBase,
        max_seq_len: int,
        pad_tok_id: int,
        num_fewshot: int,
        destination_path: str,
        fewshot_random_seed: int = 1234,
        prompt_string: str = '',
        example_delimiter: str = '\n',
        continuation_delimiter: str = ' ',
        prelimiter: str = '',
        answer_key: str = 'answer',
        strip_dataset: bool = True,
        tokenize_labels: bool = True,
        padding_size: Optional[int] = None,
        batch_mapping: Optional[dict] = None,
        hf_loading_vars: Optional[dict] = None,
        hf_parsing_map: Optional[dict] = None,
        generation_kwargs: Optional[dict] = None,
        list_keys: Optional[list] = None,
        choices_key: str = 'context_options',
    ):
        static_keys = ['mode']
        tensor_keys = ['input_ids', 'labels', 'attention_mask']
        list_of_tensors_keys = ['continuation_indices']
        super().__init__(
            dataset_uri=dataset_uri,
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            pad_tok_id=pad_tok_id,
            num_fewshot=num_fewshot,
            fewshot_random_seed=fewshot_random_seed,
            prompt_string=prompt_string,
            example_delimiter=example_delimiter,
            continuation_delimiter=continuation_delimiter,
            destination_path=destination_path,
            prelimiter=prelimiter,
            answer_key=answer_key,
            strip_dataset=strip_dataset,
            tokenize_labels=tokenize_labels,
            padding_size=padding_size,
            batch_mapping=batch_mapping,
            hf_loading_vars=hf_loading_vars,
            hf_parsing_map=hf_parsing_map,
            generation_kwargs=generation_kwargs,
            list_keys=list_keys,
            # specific to ICL dataset
            choices_key=choices_key,
            context_key=choices_key,
            static_keys=static_keys,
            tensor_keys=tensor_keys,
            list_of_tensors_keys=list_of_tensors_keys,
        )
        self.base_batch = {
            'input_ids': [],
            'continuation_indices': [],
            'mode': 'icl_task',
            'labels': [],
            'gold_indices': [],
            'choice_groupings': [],
        }

    def get_effective_batch_size(self, batch_size: int) -> int:
        batch_size = max(self.num_choices, batch_size)
        effective_batchsize = batch_size // self.num_choices
        return effective_batchsize

    def construct_context(
        self,
        example: dict[str, Any],
        preceding_text: str = '',
        add_answer: bool = False,
    ) -> str:
        """Takes a example and constructs a context with the correct context.

        for.

        the example's continuation.

        Args:
            example (Dict): The example from which to construct the context
            preceding_text (str): Any preceding text, needed to if self.example_delimiter is needed at the beginning
            add_answer (bool): This will always be true when calling this function for SchemaTaskDataset

        Returns:
            str: The single correct context for a given continuation
        """
        context_options = example[self.choices_key]
        gold_idx = example['gold']
        continuation = example['continuation']
        context = context_options[gold_idx]
        if len(preceding_text) > 0:
            context = f'{self.example_delimiter}{context}'
        context = f'{self.prelimiter}{context}{self.continuation_delimiter}{continuation}'
        return context

    def _construct_multiple_contexts(
        self,
        example: dict,
        preceding_text: str = '',
    ) -> list[str]:
        """Takes a example and constructs all contexts.

        Optionally, appends this to preceding text (such as a prompt or fewshot examples).

        Args:
            example (Dict): The example from which to construct the context
            preceding_text (str): Any preceding text, needed to if self.example_delimiter is needed at the beginning

        Returns:
            list: All context options for the selected example with formatting
        """
        context_options = example[self.choices_key]
        if len(preceding_text) > 0:
            if self.strip_data:
                cont_del = self.continuation_delimiter.rstrip()
            else:
                cont_del = self.continuation_delimiter
            context_options = [
                f'{self.prelimiter}{self.example_delimiter}{c}{cont_del}'
                for c in context_options
            ]
        else:
            context_options = [f'{self.prelimiter}{c}' for c in context_options]
        return context_options

    def _prep_example(
        self,
        example: dict,
        example_idx: int,
        num_fewshot: int,
        prompt_string: str,
        fewshot_rng: random.Random,
    ) -> dict[str, Any]:
        """Prepares a single example from a HF Dataset into tokenized format.

        with prompt and fewshot examples.

        Each task consists of multiple contexts and a single, correct continuation. Will prepend fewshot examples and
        prompt if present.

        Args:
            example (Dict): A dictionary from the hf dataset
            example_idx (int): The index of example
            num_fewshot (int): Number of examples context/continuation pairs to prepend to the test pair
            prompt_string (str): The prompt to prepend to all inputs
            fewshot_rng (random.Random): Random number generator to use for fewshot sampling

        Returns:
            Dict: Contains a dictionary with the tokenized data
        """
        prompt_and_fewshot = self._generate_few_shot_prompt(
            num_fewshot,
            example_idx,
            prompt_string,
            fewshot_rng,
        )
        ctxt = self._construct_multiple_contexts(example, prompt_and_fewshot)
        tokenized_example = self.tokenize_example(
            prompt_and_fewshot,
            ctxt,
            example,
        )
        return tokenized_example

    def tokenize_example(
        self,
        prompt_and_fewshot: str,
        context_options: list[str],
        example: dict,
    ) -> dict[str, Any]:
        """Runs text through the tokenizer and handle special cases.

        Args:
            prompt_and_fewshot (str): The collection of the prompt and fewshot examples that belongs before the example's context
            context_options (str): A list of contexts for this specific example.
            example (Dict): The example as a dictionary.

        Returns:
            Dict: Dictionary with the tokenized data
        """
        tokenized_example = {}
        preamble = self.tokenizer(prompt_and_fewshot)['input_ids']
        assert isinstance(preamble, list)
        preamble = self._fix_eos_on_preamble(preamble)
        encoded_contexts = [
            preamble
            +  # pyright: ignore[reportOperatorIssue, reportGeneralTypeIssues]
            self.tokenizer(c, add_special_tokens=False)[
                'input_ids']  # pyright: ignore[reportOperatorIssue, ]
            for c in context_options
        ]
        continuation = example['continuation']
        if self.prefix_space:
            continuation = (
                f' {continuation}'
                if not continuation.startswith(' ') else continuation
            )
        tokenized_continuation = self.tokenizer(
            continuation,
            add_special_tokens=False,
        )['input_ids']

        tokenized_example[self.context_key] = []
        tokenized_example['continuation_indices'] = []
        tokenized_example[self.answer_key] = []
        for context in encoded_contexts:
            assert isinstance(context, list)
            assert isinstance(tokenized_continuation, list)
            trimmed_context = trim_context(
                context,
                tokenized_continuation,
                self.padding_size,
            )
            assert isinstance(trimmed_context, list)
            continuation_indices = get_continuation_span(
                trimmed_context,
                tokenized_continuation,
            )
            padded_context = make_padded_input(
                trimmed_context,
                tokenized_continuation,
                self.padding_size,
                self.pad_tok_id,
                self.padding_side,
            )
            tokenized_example[self.context_key].append(padded_context)
            tokenized_example['continuation_indices'].append(
                continuation_indices,
            )
            tokenized_example[self.answer_key].append(tokenized_continuation)

        tokenized_example['gold'] = example['gold']
        return tokenized_example


def build_icl_dataloader(
    icl_task_type: str,
    dataset_uri: str,
    tokenizer: transformers.PreTrainedTokenizerBase,
    batch_size: int,
    hf_loading_vars: dict,
    hf_parsing_map: dict,
    destination_path: str = '',
    kwargs: Optional[dict[str, Any]] = None,
) -> DataSpec:
    """Factory method that builds the specific dataset for the specified.

    icl_task_type. See documentation for `get_icl_task_dataloader` for argument
    documentation.

    When writing a dataset for a new task, here you will need to:
        1. add the dataset to the factory and choose an appropriate string
        2. set the batch size for that task (see InContextLearningMultipleChoiceTaskDataset for why
            this might be different)
        3. set the `split_batch` function if necessary
    """
    # Add named parameters to kwargs
    if kwargs is None:
        kwargs = {}
    kwargs.update({
        'dataset_uri': dataset_uri,
        'tokenizer': tokenizer,
        'hf_loading_vars': hf_loading_vars,
        'hf_parsing_map': hf_parsing_map,
        'destination_path': destination_path,
    })
    dataset = construct_from_registry(
        name=icl_task_type,
        registry=registry.icl_datasets,
        partial_function=False,
        pre_validation_function=None,
        post_validation_function=None,
        kwargs=kwargs,
    )
    sampler = dist.get_sampler(dataset, drop_last=False, shuffle=False)

    return DataSpec(
        DataLoader(
            dataset,
            batch_size=dataset.get_effective_batch_size(batch_size),
            sampler=sampler,
            collate_fn=dataset.collate_fn,
        ),
        get_num_samples_in_batch=dataset.get_num_samples_in_batch,
        split_batch=dataset.split_batch,
    )


def partition_dataset_by_category(
    dataset_uri: str,
    destination_path: str,
    hf_loading_vars: dict,
    hf_parsing_map: dict,
) -> dict[str, str]:
    """If has_categories is enabled, we partition the dataset into a separate.

    dataset for each category value in the data and write each partition to a
    local file.

    Args:
        dataset_uri (str): Location of dataset.
        destination_path (str): Base destination path, we will write a separate partition off this URI for each category.
        hf_loading_vars (Dict): A dictionary containing keyword arguments to be passed into `load_dataset` if dataset is being pulled from HF.
        hf_parsing_map (Dict): A dictionary containing a mapping from HF columns to ICL dataset keys. The dictionary should be formatted {icl_key:[hf_key1, hf_key1]}.
            Column contents will be concatenated with ' ' separating them. If not included, will load the columns already present in the HF dataset.


    Raises:
        MissingConditionalImportError: If datasets not installed raise exception.
        Exception: If 'category' key missing from dataset, raise exception.

    Returns:
        Dict[str, str]: Mapping of category names to partitioned dataset local files names.
    """
    if dataset_uri.startswith('hf://'):
        dataset_uri = dataset_uri.replace('hf://', '')
        dataset = load_dataset(dataset_uri, **hf_loading_vars)
        assert isinstance(dataset,
                          HFDataset) or isinstance(dataset, IterableDataset)
        if hf_parsing_map:
            dataset_parsing_func = lambda example: {
                k: ' '.join([str(example[col]) for col in v])
                for k, v in hf_parsing_map.items()
            }
            assert hasattr(dataset, 'column_names')
            dataset = dataset.map(
                dataset_parsing_func,
                remove_columns=dataset.column_names,
            )
    else:
        with dist.local_rank_zero_download_and_wait(destination_path):
            if dist.get_local_rank() == 0:
                get_file(dataset_uri, destination_path, overwrite=True)
        dataset = load_dataset(
            'json',
            data_files=destination_path,
            split='train',
            streaming=False,
        )
    assert isinstance(dataset,
                      HFDataset) or isinstance(dataset, IterableDataset)
    assert hasattr(dataset, 'features')
    assert dataset.features is not None
    if 'category' not in dataset.features.keys():
        raise Exception(
            f"""Attempted to partition dataset by `category` \
            but it doesn't have a `category` key. \
            Got keys: {str(list(dataset.features.keys()))}""",
        )
    categories = sorted(
        set(
            dataset['category'],
        ),
    )  # pyright: ignore[reportIndexIssue, reportGeneralTypeIssues]
    output_files = {}
    for cat in categories:
        path = destination_path.split('/')
        cat_dest = '/'.join(path[:-1]) + f'/{cat}_{path[-1]}'
        tmp_path_to_broadcast = str(os.path.abspath(cat_dest))
        gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
        if dist.get_local_rank() == 0:
            subset = [
                l for l in dataset if
                l['category'] == cat  # pyright: ignore[reportGeneralTypeIssues]
            ]  # pyright: ignore[reportArgumentType, reportCallIssue]
            with open(gathered_paths[0], 'w', encoding='utf8') as f:
                for l in subset:
                    f.write(json.dumps(l, ensure_ascii=False) + '\n')
        output_files[cat] = cat_dest
    return output_files


def get_icl_task_dataloader(
    icl_task_type: str,
    dataset_uri: str,
    tokenizer: PreTrainedTokenizerBase,
    batch_size: int,
    has_categories: bool = False,
    hf_loading_vars: Optional[dict] = None,
    hf_parsing_map: Optional[dict] = None,
    destination_path: str = '',
    kwargs: Optional[dict[str, Any]] = None,
) -> Union[DataSpec, dict[str, DataSpec]]:
    r"""Constructs a dataloader (or dataloaders if has_categories is True)

    capable of evaluating LLMs on in-context learning language modeling tasks,
    for example LAMBADA. An example usage is below:

        .. testsetup::

            import transformers
            from composer.models import HuggingFaceModel
            from composer.trainer import Trainer
            dataset_uri = "/tmp/dataset_uri.jsonl"
            dataset = RandomTextClassificationDataset(size=16, use_keys=True)
            train_dataloader = torch.utils.data.DataLoader(dataset, batch_size=8)
            hf_model, tokenizer = HuggingFaceModel.hf_from_composer_checkpoint('composer-hf-checkpoint.pt')
            # At this point, hf_model is randomly initialized
            composer_model = HuggingFaceModel(hf_model, hf_tokenizer)

    Example:
        .. testcode::


            dl = get_icl_task_dataloader(
                'language_modeling',
                dataset_uri,
                tokenizer,
                batch_size=2,
                max_seq_len=2048,
                pad_tok_id=tokenizer.pad_token_id,
                num_fewshot=10,
                prompt_string='translate english to french',
                example_delimiter='\\n',
                continuation_delimiter=''
                )
            eval_evaluator = Evaluator(
                    label="lambada",
                    dataloader=dl,
                    metric_names=['InContextLearningLMAccuracy']
                )
            trainer = Trainer(
                    model=model,
                    train_dataloader=train_dataloader,
                    eval_dataloader=eval_evaluator,
                    optimizers=optimizer,
                    max_duration="1ep",
                )

    Args:
        icl_task_type (str): Name of icl_task type. One of ['multiple_choice', 'schema', 'language_modeling', 'generation_task_with_answers', 'code_evaluation']
        dataset_uri (str): A local path, a remote path beginning with ``s3://`` or another backend, or a HuggingFace dataset uri prepended with ``hf://``.
            Alternate backends must be supported by :meth:`composer.utils.maybe_create_object_store_from_uri`.
            A local dataset must consist of rows of JSON data points with task dependant fields.
            The default keys expected are "context" and "answer".
        tokenizer (transformers.PreTrainedTokenizerBase): The tokenizer used to map between strings and token ids.
        batch_size (int): Size of a batch used for eval
        has_categories: (bool): If ``True``, we will search the dataset file for a category key, and partition the dataset into a separate dataloader for each category occurring in the data.
        hf_loading_vars (Dict, default = None): A dictionary containing keyword arguments to be passed into `load_dataset` if dataset is being pulled from HF.
        hf_parsing_map (Dict, default = None): A dictionary containing a mapping from HF columns to ICL dataset keys. The dictionary should be formatted {icl_key:[hf_key1, hf_key1]}.
            Column contents will be concatenated with ' ' separating them. If not included, will load the columns already present in the HF dataset.
        destination_path: Where the dataloader will be saved.
        kwargs (Dict[str, Any], default=None): Dictionary containing a mapping from ICL dataset constructor's parameter names and their desired values.

    Returns:
        DataLoader: A dataloader used for performing in-context learning evaluation on the dataset provided.
    """
    if hf_loading_vars is None:
        hf_loading_vars = {}
    if hf_parsing_map is None:
        hf_parsing_map = {}
    if has_categories:
        result_dls = {}
        output_files = partition_dataset_by_category(
            dataset_uri,
            destination_path,
            hf_loading_vars,
            hf_parsing_map,
        )
        categories = sorted(output_files.keys())
        for category in categories:
            partition_uri = output_files[category]
            result_dls[category] = build_icl_dataloader(
                icl_task_type=icl_task_type,
                dataset_uri=partition_uri,
                tokenizer=tokenizer,
                batch_size=batch_size,
                destination_path=partition_uri + '_tmp',
                hf_loading_vars=hf_loading_vars,
                hf_parsing_map=hf_parsing_map,
                kwargs=kwargs,
            )
        return result_dls
    else:
        return build_icl_dataloader(
            icl_task_type=icl_task_type,
            dataset_uri=dataset_uri,
            tokenizer=tokenizer,
            batch_size=batch_size,
            hf_loading_vars=hf_loading_vars,
            hf_parsing_map=hf_parsing_map,
            destination_path=destination_path,
            kwargs=kwargs,
        )
