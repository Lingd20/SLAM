import torch
import os
import logging
import torch.nn.functional as F
from slam_llm.models.slam_model import (
    slam_model,
    setup_tokenizer,
    setup_encoder,
    setup_encoder_projector,
    setup_llm,
)
from slam_llm.utils.train_utils import print_model_size
from typing import List, Optional
from slam_llm.utils.metric import compute_accuracy
from transformers import T5ForConditionalGeneration


logger = logging.getLogger(__name__)

def model_factory(train_config, model_config, **kwargs):
    # return necessary components for training
    tokenizer = setup_tokenizer(train_config, model_config, **kwargs)

    encoder = setup_encoder(train_config, model_config, **kwargs)

    # llm
    llm = setup_llm(train_config, model_config, **kwargs)

    # projector
    encoder_projector = setup_encoder_projector(
        train_config, model_config, **kwargs
    )

    model = slam_model_s2s(
        encoder,
        llm,
        encoder_projector,
        tokenizer,
        train_config,
        model_config,
        **kwargs,
    )

    ckpt_path = kwargs.get(
        "ckpt_path", None
    )  # FIX(MZY): load model ckpt(mainly projector, related to model_checkpointing/checkpoint_handler.py: save_model_checkpoint_peft)
    if ckpt_path is not None:
        logger.info("loading other parts from: {}".format(ckpt_path))
        ckpt_dict = torch.load(ckpt_path, map_location="cpu")
        model.load_state_dict(ckpt_dict, strict=False)              # TODO: 这里需要测试存储的 llm 有没有全部加载进来

    print_model_size(
        model,
        train_config,
        (
            int(os.environ["RANK"])
            if train_config.enable_fsdp or train_config.enable_ddp
            else 0
        ),
    )
    return model, tokenizer


class slam_model_s2s(slam_model):
    def __init__(
        self,
        encoder,
        llm,
        encoder_projector,
        tokenizer,
        train_config,
        model_config,
        **kwargs,
    ):
        super().__init__(
            encoder,
            llm,
            encoder_projector,
            tokenizer,
            train_config,
            model_config,
            **kwargs,
        )

        # resize llm embedding layer
        if self.model_config.vocab_config.total_vocabsize != self.llm.lm_head.weight.size(0):
            self.llm.resize_token_embeddings(self.model_config.vocab_config.total_vocabsize)


    def concat_whisper_feat(self, audio_feature, input_ids, T, task = None):
        btz = len(T)
        for j in range(btz):
            if task is None or (task[j] != "T1T2" and task[j] != "T1A2"):
                for i in range(7):
                    input_ids[j, i, 1 : T[j] + 1, :] = audio_feature[j][: T[j]].clone()
            else:
                continue
        return input_ids

    def forward(self,
                input_ids: torch.LongTensor = None,
                attention_mask: Optional[torch.Tensor] = None,
                position_ids: Optional[torch.LongTensor] = None,
                past_key_values: Optional[List[torch.FloatTensor]] = None,
                inputs_embeds: Optional[torch.FloatTensor] = None,
                labels: Optional[torch.LongTensor] = None,
                use_cache: Optional[bool] = None,
                output_attentions: Optional[bool] = None,
                output_hidden_states: Optional[bool] = None,
                return_dict: Optional[bool] = None,
                **kwargs,
                ):
        audio_mel = kwargs.get("audio_mel", None)
        audio_mel_post_mask = kwargs.get("audio_mel_post_mask", None) # 2x downsample for whisper

        audio = kwargs.get("audio", None)
        audio_mask = kwargs.get("audio_mask", None)

        modality_mask = kwargs.get("modality_mask", None)

        encoder_outs = None
        if audio_mel is not None or audio is not None:
            if self.train_config.freeze_encoder: # freeze encoder
                self.encoder.eval()

            if self.model_config.encoder_name == "whisper":
                encoder_outs = self.encoder.extract_variable_length_features(audio_mel.permute(0, 2, 1)) # bs*seq*dim
            if self.model_config.encoder_name == "wavlm":
                encoder_outs = self.encoder.extract_features(audio, 1 - audio_mask) #(FIX:MZY): 1-audio_mask is needed for wavlm as the padding mask
            if self.model_config.encoder_name == "hubert":
                results = self.encoder(source = audio, padding_mask = 1-audio_mask)
                if self.model_config.encoder_type == "pretrain":
                    encoder_outs, audio_mel_post_mask = results["x"], results["padding_mask"]
                if self.model_config.encoder_type == "finetune":
                    encoder_outs, audio_mel_post_mask = results["encoder_out"], results["padding_mask"]
                    encoder_outs = encoder_outs.transpose(0, 1)
            if self.encoder is None:
                encoder_outs = audio_mel if audio_mel is not None else audio

            if self.model_config.encoder_projector == "q-former":
                encoder_outs = self.encoder_projector(encoder_outs, audio_mel_post_mask)
            if self.model_config.encoder_projector == "linear":
                encoder_outs = self.encoder_projector(encoder_outs)
            if self.model_config.encoder_projector == "cov1d-linear": 
                encoder_outs = self.encoder_projector(encoder_outs)

        if input_ids is not None:
            input_ids[input_ids == -1] = 0

            if isinstance(self.llm, T5ForConditionalGeneration):
                inputs_embeds = self.llm.shared(input_ids)
            else:
                if hasattr(self.llm.model, "embed_tokens"):
                    inputs_embeds = self.llm.model.embed_tokens(input_ids)  # [btz, 8, seq_length, emb_dim]
                elif hasattr(self.llm.model.model, "embed_tokens"):
                    inputs_embeds = self.llm.model.model.embed_tokens(input_ids)
                else:
                    inputs_embeds = self.llm.model.model.model.embed_tokens(input_ids)

            # if audio_mel is not None or audio is not None:
            #     inputs_embeds = self.concat_whisper_feat(encoder_outs, inputs_embeds, audio_length) # embed the audio feature into the input_embeds

        if modality_mask is not None:
            modality_mask = modality_mask.unsqueeze(1).repeat(1, 7, 1)  # [btz, 8, seq_length]
            modality_mask_start_indices = (modality_mask == True).float().argmax(dim=2)
            modality_lengths = torch.clamp(modality_mask.sum(dim=2), max=encoder_outs.shape[1]).tolist()

            encoder_outs_pad = torch.zeros_like(inputs_embeds)
            for i in range(encoder_outs.shape[0]):
                for j in range(7):
                    start_idx = modality_mask_start_indices[i, j].item()
                    length = modality_lengths[i][j]
                    encoder_outs_pad[i, j, start_idx:start_idx+length] = encoder_outs[i, :length]
            
            inputs_embeds[:, :7, :, :] = encoder_outs_pad[:, :7, :, :] + inputs_embeds[:, :7, :, :] * (~modality_mask[:, :, :, None])
        
        inputs_embeds = torch.mean(inputs_embeds, dim=1)  # [btz, seq_length, emb_dim], average over the 8 layers

        if kwargs.get("inference_mode", False):
            return inputs_embeds, attention_mask

        text_labels = labels[:, 7] if labels is not None else None
        audio_labels = labels[:, :7] if labels is not None else None
        model_outputs = self.llm(inputs_embeds=inputs_embeds, attention_mask=attention_mask, labels=text_labels)    # here we use the text token layer as the target label

        # parrallel generation TODO: 需要重写八层的loss，现在只有最后一层的loss
        x_ori = model_outputs.logits
        text_vocab_size = self.model_config.vocab_config.padded_text_vocabsize
        audio_vocab_size = self.model_config.vocab_config.padded_audio_vocabsize
        xt = x_ori[..., :text_vocab_size]
        xa = []
        for i in range(7):
            xa.append(x_ori[..., text_vocab_size + audio_vocab_size * i : text_vocab_size + audio_vocab_size * (i + 1)])

        total_loss = self.compute_parallel_loss(xt, text_labels, xa, audio_labels)
        model_outputs.loss = total_loss

        text_acc = -1
        if self.metric:
            with torch.no_grad():
                preds = torch.argmax(xt, -1)
                text_acc = compute_accuracy(preds.detach()[:, :-1], text_labels.detach()[:, 1:], ignore_label=-100)

        return model_outputs, text_acc


    @torch.no_grad()
    def inference(
        self,
        wav_path=None,
        prompt=None,
        generation_config=None,
        logits_processor=None,
        stopping_criteria=None,
        prefix_allowed_tokens_fn=None,
        synced_gpus=None,
        assistant_model=None,
        streamer=None,
        negative_prompt_ids=None,
        negative_prompt_attention_mask=None,
        **kwargs,
    ):
        # inference for asr model

        device = kwargs.get("device", "cuda")
        if os.path.exists(wav_path):  # Audio-Text QA
            import whisper

            audio_raw = whisper.load_audio(wav_path)
            audio_raw = whisper.pad_or_trim(audio_raw)

            mel_size = getattr(
                self.dataset_config, "mel_size", 80
            )  # 80 for large v1 and v2, 128 for large v3
            audio_mel = (
                whisper.log_mel_spectrogram(audio_raw, n_mels=mel_size)
                .permute(1, 0)[None, :, :]
                .to(device)
            )

            encoder_outs = self.encoder.extract_variable_length_features(
                audio_mel.permute(0, 2, 1)
            )

            if self.model_config.encoder_projector == "q-former":
                audio_mel_post_mask = torch.ones(
                    encoder_outs.size()[:-1], dtype=torch.long
                ).to(encoder_outs.device)
                encoder_outs = self.encoder_projector(encoder_outs, audio_mel_post_mask)
            if self.model_config.encoder_projector == "linear":
                encoder_outs = self.encoder_projector(encoder_outs)
        else:  # Text QA
            encoder_outs = torch.empty(
                1, 0, self.llm.model.embed_tokens.embedding_dim
            ).to(device)

        prompt = "USER: {}\n ASSISTANT:".format(prompt)
        prompt_ids = self.tokenizer.encode(prompt)
        prompt_length = len(prompt_ids)
        prompt_ids = torch.tensor(prompt_ids, dtype=torch.int64).to(device)

        if hasattr(self.llm.model, "embed_tokens"):
            inputs_embeds = self.llm.model.embed_tokens(prompt_ids)
        elif hasattr(self.llm.model.model, "embed_tokens"):
            inputs_embeds = self.llm.model.model.embed_tokens(prompt_ids)
        else:
            inputs_embeds = self.llm.model.model.model.embed_tokens(prompt_ids)

        inputs_embeds = torch.cat(
            (encoder_outs, inputs_embeds[None, :, :]), dim=1
        )  # [audio,prompt]

        attention_mask = torch.ones(inputs_embeds.size()[:-1], dtype=torch.long).to(
            inputs_embeds.device
        )

        # generate
        model_outputs = self.generate(
            inputs_embeds=inputs_embeds, attention_mask=attention_mask, **kwargs
        )

        return model_outputs


    def compute_parallel_loss(self, xt, text_labels, xa, audio_labels):
        """
        Compute the parallel loss for text and audio layers.
        """
        text_vocab_size = self.model_config.vocab_config.padded_text_vocabsize
        audio_vocab_size = self.model_config.vocab_config.padded_audio_vocabsize
        
        if text_labels is not None:
            # text_loss = F.cross_entropy(xt.reshape(-1, text_vocab_size), text_labels.reshape(-1), ignore_index=-100)
            text_loss = F.cross_entropy(xt[:, :-1, :].reshape(-1, text_vocab_size), text_labels[:, 1:].reshape(-1), ignore_index=-100)
        else:
            text_loss = 0

        audio_loss = 0
        for i in range(7):
            if audio_labels[:,i] is not None:
                # audio_loss += F.cross_entropy(xa[i].reshape(-1, audio_vocab_size), audio_labels[:,i].reshape(-1), ignore_index=-100)
                audio_loss += F.cross_entropy(xa[i][:, :-1, :].reshape(-1, audio_vocab_size), audio_labels[:, i, 1:].reshape(-1), ignore_index=-100)

        total_loss = (text_loss + audio_loss) / 8

        return total_loss