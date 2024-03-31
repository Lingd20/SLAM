#!/bin/bash
#export PYTHONPATH=/root/whisper:$PYTHONPATH
export HF_ENDPOINT=https://hf-mirror.com
export CUDA_VISIBLE_DEVICES=2
export TOKENIZERS_PARALLELISM=false
# export CUDA_LAUNCH_BLOCKING=1

cd /root/SLAM-LLM

# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/tiny.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/base.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/small.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/medium.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/large-v2.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/Whisper/large-v2-qwen.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/wavlm/WavLM-Base.pt
# speech_encoder_path=/nfs/maziyang.mzy/models/wavlm/WavLM-Large.pt
audio_encoder_path=/root/models/BEATs_iter3_plus_AS2M.pt  # pretrain
# audio_encoder_path=/root/models/BEATs_iter3_plus_AS2M_finetuned_on_AS2M_cpt2.pt  # finetune


# llm_path=/nfs/maziyang.mzy/models/TinyLlama-1.1B-intermediate-step-1431k-3T
# llm_path=/nfs/maziyang.mzy/models/TinyLlama-1.1B-Chat-v0.4
# llm_path=/nfs/maziyang.mzy/models/phi-2
# llm_path=/nfs/zhifu.gzf/ckpt/Llama-2-7b-hf
# llm_path=/nfs/maziyang.mzy/models/Llama-2-7b-chat-hf
llm_path=/root/models/vicuna-7b-v1.5
# llm_path=/nfs/maziyang.mzy/models/vicuna-7b-v1.5
# llm_path=/nfs/maziyang.mzy/models/vicuna-13b-v1.5

output_dir=/root/exps/lora_test
ckpt_path=$output_dir/aac/3
val_data_path=/root/data/AudioCaps/new_test.jsonl
decode_log=$ckpt_path/decode_log_test_clean_beam4_repetition_penalty1

# -m debugpy --listen 6666 --wait-for-client
python src/llama_recipes/pipeline/inference_batch.py \
    --config-path "/root/SLAM-LLM/scripts/conf" \
    --config-name "aac_vicuna_lora.yaml" \
    hydra.run.dir=$ckpt_path \
    model_config.llm_name="vicuna-7b-v1.5" \
    model_config.llm_path=$llm_path \
    model_config.llm_dim=4096 \
    model_config.encoder_name=beats \
    model_config.encoder_path=$audio_encoder_path \
    model_config.encoder_dim=768 \
    model_config.encoder_projector=linear \
    model_config.encoder_projector_ds_rate=5 \
    +model_config.normalize=true \
    dataset_config.dataset=audio_dataset \
    +dataset_config.prompt="Describe the audio you hear. Output the audio caption directly without redundant content. Ensure that the output is not duplicated." \
    dataset_config.val_data_path=$val_data_path \
    dataset_config.fbank_mean=15.41663 \
    dataset_config.fbank_std=6.55582 \
    dataset_config.inference_mode=true \
    +dataset_config.normalize=true \
    +dataset_config.input_type=mel \
    train_config.model_name=aac \
    train_config.batching_strategy=custom \
    train_config.num_epochs=1 \
    train_config.val_batch_size=8 \
    train_config.num_workers_dataloader=4 \
    train_config.output_dir=$output_dir \
    +decode_log=$decode_log \
    train_config.freeze_encoder=true \
    train_config.freeze_llm=true \
    train_config.use_peft=true \
    train_config.peft_config.peft_method=lora \
    +ckpt_path=$ckpt_path/model.pt \
    +peft_ckpt=$ckpt_path \
# ++model_config.encoder_projector=q-former \
# ++dataset_config.fix_length_audio=64 \
# --use_peft --peft_method lora \