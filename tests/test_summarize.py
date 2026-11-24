import json

from ata.config import OllamaConfig
from ata.summarize import (
    EMPTY,
    _merge_partials,
    format_window,
    loads_lenient,
    normalize_payload,
    pick_num_ctx,
    split_windows,
    summarize_meeting,
)


def _seg(start, source, text):
    return {"start": start, "end": start + 3, "source": source, "text": text}


def test_split_windows_by_minutes():
    segs = [_seg(i * 60, "me", f"linha {i}") for i in range(25)]
    windows = split_windows(segs, window_minutes=10)
    assert len(windows) == 3
    assert [len(w) for w in windows] == [10, 10, 5]


def test_format_window_merges_same_speaker():
    segs = [_seg(0, "me", "oi"), _seg(3, "me", "tudo bem"), _seg(6, "system", "bem")]
    text = format_window(segs)
    assert text == "ME: oi tudo bem\nSYSTEM: bem"


def test_loads_lenient_repairs_wrapped_json():
    assert loads_lenient('lixo antes {"summary": "x"} lixo depois') == {"summary": "x"}
    assert loads_lenient("nada de json") == {}


def test_normalize_payload_action_items_shapes():
    raw = {"summary": " s ", "action_items": ["fazer X", {"task": "Y", "owner": "", "deadline": "sexta"}],
           "other_notes": "único", "agenda_items": [{"topic": " Roadmap ", "discussion": " ok "}]}
    out = normalize_payload(raw)
    assert out["summary"] == "s"
    assert out["other_notes"] == ["único"]
    assert out["action_items"][0] == {"task": "fazer X", "owner": None, "deadline": None}
    assert out["action_items"][1] == {"task": "Y", "owner": None, "deadline": "sexta"}
    assert out["agenda_items"] == [{"topic": "Roadmap", "discussion": "ok"}]


def test_empty_has_new_schema_keys_with_safe_defaults():
    assert EMPTY["participants"] == []
    assert EMPTY["decisions"] == []
    assert EMPTY["risks"] == []
    assert EMPTY["cloud_providers"] == []
    assert EMPTY["technical_points"] == {"services": [], "architecture": [], "dependencies": []}


def test_normalize_payload_new_list_fields():
    raw = {
        "participants": [" Ana ", "", "Bruno"],
        "decisions": ["Adiar o release"],
        "risks": [" Atraso do fornecedor "],
        "cloud_providers": ["AWS", " "],
    }
    out = normalize_payload(raw)
    assert out["participants"] == ["Ana", "Bruno"]
    assert out["decisions"] == ["Adiar o release"]
    assert out["risks"] == ["Atraso do fornecedor"]
    assert out["cloud_providers"] == ["AWS"]


def test_normalize_payload_missing_new_fields_defaults_to_empty():
    out = normalize_payload({"summary": "só isso"})
    assert out["participants"] == [] and out["decisions"] == [] and out["risks"] == []
    assert out["cloud_providers"] == []
    assert out["technical_points"] == {"services": [], "architecture": [], "dependencies": []}


def test_normalize_payload_technical_points_well_formed():
    raw = {"technical_points": {"services": ["S3", " "], "architecture": ["Event-driven"],
                                "dependencies": []}}
    out = normalize_payload(raw)
    assert out["technical_points"] == {
        "services": ["S3"], "architecture": ["Event-driven"], "dependencies": [],
    }


def test_normalize_payload_technical_points_malformed_never_crashes():
    for bad in ("uma string qualquer", ["lista", "solta"], None, {}, {"services": "S3"}):
        out = normalize_payload({"technical_points": bad})
        assert out["technical_points"] == {"services": [] if bad != {"services": "S3"} else ["S3"],
                                           "architecture": [], "dependencies": []}


def test_normalize_payload_technical_points_missing_key_defaults_empty():
    out = normalize_payload({"technical_points": {"services": ["X"]}})  # sem architecture/dependencies
    assert out["technical_points"] == {"services": ["X"], "architecture": [], "dependencies": []}


def test_merge_partials_dedupes_new_simple_fields():
    partials = [
        {"participants": ["Ana", "Bruno"], "decisions": ["Adiar release"], "risks": ["Atraso"],
         "cloud_providers": ["AWS"]},
        {"participants": ["ana"], "decisions": ["Adiar release"], "risks": ["Outro risco"],
         "cloud_providers": ["aws", "GCP"]},
    ]
    out = _merge_partials(partials)
    # dedupe é por fingerprint exato (lowercase) do item, não fuzzy — "Ana" e "ana" colidem
    assert out["participants"] == ["Ana", "Bruno"]
    assert out["decisions"] == ["Adiar release"]
    assert out["risks"] == ["Atraso", "Outro risco"]
    assert out["cloud_providers"] == ["AWS", "GCP"]


def test_merge_partials_merges_technical_points_subkeys_without_duplicating():
    partials = [
        {"technical_points": {"services": ["S3"], "architecture": [], "dependencies": ["API legada"]}},
        {"technical_points": {"services": ["S3", "Lambda"], "architecture": ["Event-driven"], "dependencies": []}},
    ]
    out = _merge_partials(partials)
    assert out["technical_points"] == {
        "services": ["S3", "Lambda"],
        "architecture": ["Event-driven"],
        "dependencies": ["API legada"],
    }


def test_merge_partials_handles_missing_technical_points_gracefully():
    out = _merge_partials([{"summary": "x"}, {"technical_points": None}])
    assert out["technical_points"] == {"services": [], "architecture": [], "dependencies": []}


def test_pick_num_ctx_within_bounds():
    cfg = OllamaConfig(num_ctx_min=4096, num_ctx_max=32768)
    assert pick_num_ctx("x" * 10, cfg) == 4096
    assert pick_num_ctx("x" * 500_000, cfg) == 32768


from ata.summarize import OllamaUnavailable


class FakeClient:
    def __init__(self, *, reduce_ok=True, fail_map_at=None):
        self.reduce_ok = reduce_ok
        self.fail_map_at = fail_map_at
        self.calls = []
        self._map_calls = 0

    def list_models(self):
        return [{"name": "llama3.2:3b", "capabilities": ["completion"]}]

    def generate(self, model, prompt, *, num_ctx, as_json=True):
        self.calls.append(prompt[:20])
        if "RESUMOS PARCIAIS" in prompt:
            if not self.reduce_ok:
                return "quebrado"
            return json.dumps({"summary": "reunião consolidada",
                               "agenda_items": [{"topic": "t", "discussion": "decidiu A"}],
                               "action_items": [], "next_steps": "", "other_notes": []})
        self._map_calls += 1
        if self.fail_map_at and self._map_calls >= self.fail_map_at:
            raise OllamaUnavailable("boom")
        return json.dumps({"summary": "parcial",
                           "agenda_items": [{"topic": "t", "discussion": "decidiu A"}],
                           "action_items": [], "next_steps": "", "other_notes": []})


def _segments():
    return [_seg(i * 60, "me" if i % 2 else "system", f"fala {i}") for i in range(25)]


def test_summarize_meeting_map_reduce_happy():
    cfg = OllamaConfig(window_minutes=10)
    payload, diag = summarize_meeting(_segments(), cfg, client=FakeClient())
    assert diag["status"] == "ok"
    assert diag["windows"] == 3
    assert payload["summary"] == "reunião consolidada"
    assert payload["agenda_items"] == [{"topic": "t", "discussion": "decidiu A"}]


def test_summarize_meeting_deterministic_fallback_when_reduce_breaks():
    cfg = OllamaConfig(window_minutes=10)
    payload, diag = summarize_meeting(_segments(), cfg, client=FakeClient(reduce_ok=False))
    assert diag["status"] == "ok"
    assert "determinística" in " ".join(diag["steps"])
    # deduplicado/mesclado das 3 janelas, mesmo tópico "t"
    assert payload["agenda_items"] == [{"topic": "t", "discussion": "decidiu A decidiu A decidiu A"}]


def test_summarize_meeting_no_segments():
    payload, diag = summarize_meeting([], OllamaConfig(), client=FakeClient())
    assert diag["status"] == "skipped"
    assert payload["summary"] == ""


def test_summarize_meeting_partial_failure_is_not_silent_ok():
    cfg = OllamaConfig(window_minutes=10)
    payload, diag = summarize_meeting(_segments(), cfg, client=FakeClient(fail_map_at=3))
    assert diag["status"] == "partial"          # NÃO vira "ok" só porque há algum conteúdo
    assert diag["covered_windows"] == 2 and diag["windows"] == 3
    assert "de fora" in diag["reason"]
    assert payload["agenda_items"] == [{"topic": "t", "discussion": "decidiu A"}]  # o que deu para resumir continua lá


class FakeClientWithNewFields(FakeClient):
    """Igual ao FakeClient, mas o map/reduce também devolve as 5 chaves novas."""

    def generate(self, model, prompt, *, num_ctx, as_json=True):
        self.calls.append(prompt[:20])
        if "RESUMOS PARCIAIS" in prompt:
            if not self.reduce_ok:
                return "quebrado"
            return json.dumps({
                "summary": "reunião consolidada", "agenda_items": [], "action_items": [],
                "next_steps": "", "other_notes": [],
                "participants": ["Ana", "Bruno"], "decisions": ["Adiar release"],
                "risks": ["Atraso do fornecedor"],
                "technical_points": {"services": ["S3"], "architecture": [], "dependencies": []},
                "cloud_providers": ["AWS"],
            })
        self._map_calls += 1
        return json.dumps({
            "summary": "parcial", "agenda_items": [], "action_items": [], "next_steps": "",
            "other_notes": [], "participants": ["Ana"], "decisions": ["Adiar release"],
            "risks": ["Atraso do fornecedor"],
            "technical_points": {"services": ["S3"], "architecture": [], "dependencies": []},
            "cloud_providers": ["AWS"],
        })


def test_summarize_meeting_carries_new_schema_fields_through_map_reduce():
    cfg = OllamaConfig(window_minutes=10)
    payload, diag = summarize_meeting(_segments(), cfg, client=FakeClientWithNewFields())
    assert diag["status"] == "ok"
    assert payload["participants"] == ["Ana", "Bruno"]
    assert payload["decisions"] == ["Adiar release"]
    assert payload["risks"] == ["Atraso do fornecedor"]
    assert payload["cloud_providers"] == ["AWS"]
    assert payload["technical_points"] == {"services": ["S3"], "architecture": [], "dependencies": []}


def test_summarize_meeting_deterministic_fallback_merges_new_fields():
    cfg = OllamaConfig(window_minutes=10)
    payload, diag = summarize_meeting(_segments(), cfg,
                                      client=FakeClientWithNewFields(reduce_ok=False))
    assert "determinística" in " ".join(diag["steps"])
    # fallback consolida os PARCIAIS do map (não o reduce, que falhou) — cada janela só tem "Ana"
    assert payload["participants"] == ["Ana"]  # deduplicado entre as 3 janelas
    assert payload["cloud_providers"] == ["AWS"]
