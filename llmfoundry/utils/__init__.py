# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

from llmfoundry.registry import config_transforms
from llmfoundry.utils.builders import (
    add_metrics_to_eval_loaders,
    build_algorithm,
    build_callback,
    build_composer_model,
    build_eval_loaders,
    build_evaluators,
    build_icl_data_and_gauntlet,
    build_icl_evaluators,
    build_logger,
    build_metric,
    build_optimizer,
    build_scheduler,
    build_tokenizer,
)
from llmfoundry.utils.checkpoint_conversion_helpers import (
    convert_and_save_ft_weights,
    get_hf_tokenizer_from_composer_state_dict,
    load_tokenizer,
)
from llmfoundry.utils.config_utils import (
    calculate_batch_size_info,
    log_config,
    pop_config,
    process_init_device,
    update_batch_size_info,
)
from llmfoundry.utils.consts import CROSS_ENTROPY_IGNORE_INDEX
from llmfoundry.utils.data_prep_utils import (
    DownloadingIterable,
    merge_shard_groups,
)
from llmfoundry.utils.huggingface_hub_utils import \
    edit_files_for_hf_compatibility
from llmfoundry.utils.logging_utils import SpecificWarningFilter
from llmfoundry.utils.model_download_utils import (
    download_from_hf_hub,
    download_from_http_fileserver,
    download_from_oras,
)
from llmfoundry.utils.mosaicml_logger_utils import (
    find_mosaicml_logger,
    log_eval_analytics,
    log_train_analytics,
    maybe_create_mosaicml_logger,
)
from llmfoundry.utils.prompt_files import load_prompts, load_prompts_from_file
from llmfoundry.utils.registry_utils import (
    TypedRegistry,
    construct_from_registry,
    create_registry,
    import_file,
    save_registry,
)
from llmfoundry.utils.warnings import (
    ExperimentalWarning,
    VersionedDeprecationWarning,
    experimental_class,
    experimental_function,
)

config_transforms.register(
    'update_batch_size_info',
    func=update_batch_size_info,
)

__all__ = [
    'add_metrics_to_eval_loaders',
    'build_algorithm',
    'build_callback',
    'build_eval_loaders',
    'build_evaluators',
    'build_icl_data_and_gauntlet',
    'build_icl_evaluators',
    'build_logger',
    'build_optimizer',
    'build_scheduler',
    'build_tokenizer',
    'build_composer_model',
    'build_metric',
    'get_hf_tokenizer_from_composer_state_dict',
    'load_tokenizer',
    'convert_and_save_ft_weights',
    'pop_config',
    'calculate_batch_size_info',
    'update_batch_size_info',
    'process_init_device',
    'log_config',
    'DownloadingIterable',
    'merge_shard_groups',
    'edit_files_for_hf_compatibility',
    'SpecificWarningFilter',
    'download_from_http_fileserver',
    'download_from_hf_hub',
    'download_from_oras',
    'maybe_create_mosaicml_logger',
    'find_mosaicml_logger',
    'log_eval_analytics',
    'log_train_analytics',
    'load_prompts',
    'load_prompts_from_file',
    'TypedRegistry',
    'create_registry',
    'construct_from_registry',
    'import_file',
    'save_registry',
    'VersionedDeprecationWarning',
    'ExperimentalWarning',
    'experimental_function',
    'experimental_class',
    'CROSS_ENTROPY_IGNORE_INDEX',
]
