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
from slam_llm.utils.snac_utils import layershift, get_snac_answer_token
from snac import SNAC
from tqdm import tqdm


logger = logging.getLogger(__name__)

def partial_freeze_weights(model, original_vocabsize, total_vocabsize):
    trainable_range = (original_vocabsize, total_vocabsize)

    # Define a hook to zero out the gradient for weights outside the trainable range during the backward pass
    def zero_out_gradient(grad):
        grad[:trainable_range[0], :] = 0
        grad[trainable_range[1] + 1:, :] = 0
        return grad

    # Freeze all layers first
    for param in model.parameters():
        param.requires_grad = False

    # Assuming the output layer is `lm_head`
    for param in model.llm.lm_head.parameters():
        # Compute the standard deviation for He initialization
        std_dev = (2.0 / param.size(1)) ** 0.5

        # Initialize the specific rows with He initialization
        param[original_vocabsize:total_vocabsize] = (
            torch.randn((trainable_range[1] - trainable_range[0], param.size(1))) * std_dev
        )
        param.requires_grad = True

        # Register the hook on the weight tensor
        param.register_hook(zero_out_gradient)


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

    codec_decoder = None
    if model_config.codec_decode:
        codec_decoder = SNAC.from_pretrained(model_config.codec_decoder_path).eval()

    model = slam_model_s2s(
        encoder,
        llm,
        encoder_projector,
        tokenizer,
        codec_decoder,
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

    if train_config.train_audio_embed_only:
        logger.info("Only training audio embedding layer")
        partial_freeze_weights(model, model_config.vocab_config.padded_text_vocabsize, model_config.vocab_config.total_vocabsize)

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
        codec_decoder,
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
        self.original_vocabsize = self.llm.lm_head.weight.size(0)
        if self.model_config.vocab_config.total_vocabsize != self.original_vocabsize:
            self.llm.resize_token_embeddings(self.model_config.vocab_config.total_vocabsize)
            logger.info("Resize llm embedding layer's vocab size to {}".format(self.model_config.vocab_config.total_vocabsize))

        self.codec_decoder = codec_decoder

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

        loss_recorder = []
        total_loss, loss_recorder = self.compute_parallel_loss(xt, text_labels, xa, audio_labels)
        model_outputs.loss = total_loss

        text_acc = -1
        audio_acc = [-1 for _ in range(7)]
        if self.metric:
            with torch.no_grad():
                preds = torch.argmax(xt, -1)
                text_acc = compute_accuracy(preds.detach()[:, :-1], text_labels.detach()[:, 1:], ignore_label=-100)

                preds_audio = [torch.argmax(xa[i], -1) for i in range(7)]
                audio_acc = [compute_accuracy(preds_audio[i].detach()[:, :-1], audio_labels[:, i, 1:], ignore_label=-100) for i in range(7)]

        # metrics = {"text_acc": text_acc, "audio_acc": audio_acc, "layer_loss": loss_recorder}
        return model_outputs, text_acc, audio_acc, loss_recorder



    def compute_parallel_loss(self, xt, text_labels, xa, audio_labels):
        """
        Compute the parallel loss for text and audio layers.
        """
        text_vocab_size = self.model_config.vocab_config.padded_text_vocabsize
        audio_vocab_size = self.model_config.vocab_config.padded_audio_vocabsize
        layer_loss = [0 for _ in range(8) ]
        
        if text_labels is not None:
            # text_loss = F.cross_entropy(xt.reshape(-1, text_vocab_size), text_labels.reshape(-1), ignore_index=-100)
            text_loss = F.cross_entropy(xt[:, :-1, :].reshape(-1, text_vocab_size), text_labels[:, 1:].reshape(-1), ignore_index=-100)
            layer_loss[7] = text_loss
        else:
            text_loss = 0

        total_audio_loss = 0
        single_audio_loss = 0
        for i in range(7):
            if audio_labels[:,i] is not None:
                # audio_loss += F.cross_entropy(xa[i].reshape(-1, audio_vocab_size), audio_labels[:,i].reshape(-1), ignore_index=-100)
                single_audio_loss = F.cross_entropy(xa[i][:, :-1, :].reshape(-1, audio_vocab_size), audio_labels[:, i, 1:].reshape(-1), ignore_index=-100)
                layer_loss[i] = single_audio_loss
                total_audio_loss += single_audio_loss

        total_loss = (text_loss + total_audio_loss) / 8
        return total_loss, layer_loss


    @torch.no_grad()
    def generate(self,
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
        kwargs["inference_mode"] = True

        inputs_embeds, attention_mask = self.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            labels=labels,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            **kwargs,
        )

        generated_ids = [[] for _ in range(8)]
        current_input_text = None
        current_audio_tokens = [None for _ in range(7)]
        # input_pos = torch.arange(input_ids.size(-1), device=input_ids.device).unsqueeze(0)
        past_key_values = None

        do_sample = kwargs.get("do_sample", False)
        max_new_tokens = kwargs.get("max_new_tokens", 360)
        temperature = kwargs.get("temperature", 1.0)
        repetition_penalty = kwargs.get("repetition_penalty", 1.0)

        pad_t = self.model_config.vocab_config.pad_t
        pad_a = self.model_config.vocab_config.pad_a
        eot = self.model_config.vocab_config.eot
        eoa = self.model_config.vocab_config.eoa

        text_end = False     # Track whether text generation has ended
        audio_end = False    # Track whether audio generation has ended

        # NOTE: currently, we only support greedy decoding and sampling for parallel generation
        for step in tqdm(range(max_new_tokens), desc="Generating"):
            if current_input_text is not None:
                audio_tokens = torch.cat([layershift(current_audio_tokens[i], i).unsqueeze(1) for i in range(7)], dim=1)
                combined_input_ids = torch.cat([audio_tokens, current_input_text.unsqueeze(1)], dim=1)
                inputs_embeds = self.llm.model.embed_tokens(combined_input_ids)
                inputs_embeds = torch.mean(inputs_embeds, dim=1).unsqueeze(1)
            
            outputs = self.llm(
                inputs_embeds=inputs_embeds,                  # [btz, seq_len / 1, emb_dim]
                attention_mask=attention_mask,                # single sample, no need for attention mask
                past_key_values=past_key_values,
                # position_ids=input_pos,
                use_cache=True,
            )
            
            logits = outputs.logits
            past_key_values = outputs.past_key_values       # Update past_key_values for the next step

            # Split logits into text and audio layers based on vocab size
            text_vocab_size = self.model_config.vocab_config.padded_text_vocabsize
            audio_vocab_size = self.model_config.vocab_config.padded_audio_vocabsize
            xt_logits = logits[..., :text_vocab_size]
            xa_logits = [logits[..., text_vocab_size + audio_vocab_size * i : text_vocab_size + audio_vocab_size * (i + 1)] for i in range(7)]

            if repetition_penalty != 1.0:
                for token_id in set(generated_ids[7]):
                    # Apply penalty to text logits
                    if xt_logits[0, -1, token_id] < 0:
                        xt_logits[0, -1, token_id] *= repetition_penalty
                    else:
                        xt_logits[0, -1, token_id] /= repetition_penalty
                
                for i in range(7):
                    for token_id in set(generated_ids[i]):
                        # Apply penalty to audio logits
                        if xa_logits[i][0, -1, token_id] < 0:
                            xa_logits[i][0, -1, token_id] *= repetition_penalty
                        else:
                            xa_logits[i][0, -1, token_id] /= repetition_penalty

            if not text_end:
                next_token_text = self.sample_next_token(xt_logits[:, -1, :], do_sample, temperature, text_vocab_size).squeeze(0)
            else:
                next_token_text = torch.tensor([pad_t], device=input_ids.device)

            next_tokens_audio = []
            for i in range(7):
                if not audio_end:
                    next_token_audio = self.sample_next_token(xa_logits[i][:, -1, :], do_sample, temperature, audio_vocab_size).squeeze(0)
                else:
                    next_token_audio = torch.full((input_ids.size(0),), pad_a, device=input_ids.device)  # 填充pad_a
                next_tokens_audio.append(next_token_audio)

            if next_tokens_audio[-1] == eoa:
                audio_end = True
            if next_token_text == eot:
                text_end = True
            
            # Update input_ids and inputs_embeds for the next step
            current_input_text = next_token_text
            for i in range(7):
                current_audio_tokens[i] = next_tokens_audio[i]

            # if input_pos.size(-1) > 1:
            #     input_pos = torch.tensor(input_pos.size(-1), device=input_ids.device).unsqueeze(0)
            # else:
            #     input_pos = input_pos.add_(1)
            attention_mask = torch.cat([attention_mask, torch.ones((input_ids.size(0), 1), device=input_ids.device)], dim=1)

            if audio_end:
                break

            # Append generated tokens to the list
            for i in range(7):
                generated_ids[i].append(next_tokens_audio[i].clone().tolist()[0])  # Audio layers
            generated_ids[7].append(next_token_text.clone().tolist()[0])  # Text layer

        # Concatenate the generated tokens to form the complete sequence
        text_tokens = generated_ids[-1]
        generated_ids[-1] = text_tokens[: text_tokens.index(eot)] if eot in text_tokens else text_tokens
        generated_ids = [torch.tensor(layer) for layer in generated_ids] 
        return generated_ids


    @torch.no_grad()
    def sample_next_token(self, logits, do_sample, temperature, vocab_size, num_samples=1):
        """
        Generate the next token based on the model output logits.
        Both sampling and greedy decoding are supported.
        """
        if do_sample:
            return torch.multinomial(F.softmax(logits / temperature, dim=-1), num_samples=num_samples)
        else:
            return torch.argmax(logits, dim=-1, keepdim=True)