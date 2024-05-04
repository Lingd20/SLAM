# <img src="assets/bat.png" alt="SELD_SpatialSoundQA" width="25" height="20"> SELD_SpatialSoundQA

This repo hosts the code and models of "[BAT: Learning to Reason about Spatial Sounds with Large Language Models](https://arxiv.org/abs/2402.01591)" [ICML 2024 [bib](https://github.com/zszheng147/Spatial-AST#citation)].

## Performance and checkpoints
Encoder | Projector | LLM
|---|---|---
[Spatial-AST]() | Q-Former | [vicuna-7b-v1.5](https://huggingface.co/lmsys/vicuna-7b-v1.5) 

## Data preparation
You need to prepare the data jsonl in this format.
```
{"audio_id": "eval/audio/YI-HlrcP6Qg4", "reverb_id": "q9vSo1VnCiC/0.npy", "audio_id2": null, "reverb_id2": null, "question_id": 0, "question_type": "CLASSIFICATION", "question": "Enumerate the sound occurrences in the audio clip.", "answer": "accelerating, revving, vroom; car; vehicle"}
...
{"audio_id": "eval/audio/YZX2fVPmUidA", "reverb_id": "q9vSo1VnCiC/32.npy", "audio_id2": "eval/audio/YjNjUU01quLs", "reverb_id2": "q9vSo1VnCiC/31.npy", "question_id": 58, "question_type": "MIXUP_NONBINARY_DISTANCE", "question": "How far away is the sound of the banjo from the sound of the whack, thwack?", "answer": "2m"}
```

## Decode with checkpoints

## Train a new model
```
bash examples/seld_spatialsoundqa/scripts/finetune_spatial-ast_linear_vicuna_7b.sh
```

## TODO
- [ ] Decode with checkpoints
- [ ] Upload pretrained checkpoints
- [ ] 

## Citation
```
@article{zheng2024bat,
  author    = {Zheng, Zhisheng and Peng, Puyuan and Ma, Ziyang and Chen, Xie and Choi, Eunsol and Harwath, David},
  title     = {BAT: Learning to Reason about Spatial Sounds with Large Language Models},
  journal   = {arXiv preprint arXiv:2402.01591},
  year      = {2024},
}
```