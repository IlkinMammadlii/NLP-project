"""Model/tokenizer loading and logits at response positions only (saves memory: V ~ 49k)."""
import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer

from .config import MODELS
from .data import END_TOKEN
from .utils import device


def load_tokenizer(name: str = "135m"):
    tok = AutoTokenizer.from_pretrained(MODELS.get(name, name))
    if tok.chat_template is None and name != "teacher":
        # base checkpoints ship no chat template; the teacher prompt is rendered with the teacher's own
        # template (same vocabulary, checked by check_shared_tokenizer)
        tok.chat_template = AutoTokenizer.from_pretrained(MODELS["teacher"]).chat_template
    if tok.pad_token_id is None:
        tok.pad_token = "<|endoftext|>"
    return tok


def check_shared_tokenizer(student_tok, teacher_tok) -> None:
    """KD over token positions needs one vocabulary; fail loudly if the family differs."""
    assert student_tok.get_vocab() == teacher_tok.get_vocab(), "student and teacher vocabularies differ"
    assert student_tok.convert_tokens_to_ids(END_TOKEN) == teacher_tok.convert_tokens_to_ids(END_TOKEN)


def load_model(name: str, dtype=torch.bfloat16, train: bool = False):
    m = AutoModelForCausalLM.from_pretrained(MODELS.get(name, name), dtype=dtype)
    m.to(device())
    m.train(train)
    if not train:
        m.requires_grad_(False)
    return m


def response_hidden(model, batch: dict) -> torch.Tensor:
    """(N, H) final hidden states at the flat response positions of a `data.collate` batch."""
    dev = next(model.parameters()).device
    hidden = model.get_decoder()(input_ids=batch["input_ids"].to(dev),
                                 attention_mask=batch["attention_mask"].to(dev),
                                 use_cache=False).last_hidden_state
    return hidden[batch["rows"].to(dev), batch["pos"].to(dev)]


def head_logits(model, h: torch.Tensor) -> torch.Tensor:
    """fp32 output projection (outside autocast) for teacher and student: bf16 logits have a
    resolution of ~0.1 nat at |logit| ~ 20, which would quantise exactly the tail probabilities we study."""
    head = model.get_output_embeddings()
    with torch.autocast(device_type=h.device.type, enabled=False):
        return F.linear(h.float(), head.weight.float(), None if head.bias is None else head.bias.float())


def response_logits(model, batch: dict) -> torch.Tensor:
    """(N, V) fp32 logits at the flat response positions of a `data.collate` batch."""
    return head_logits(model, response_hidden(model, batch))
