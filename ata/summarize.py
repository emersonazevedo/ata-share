"""Geração da ata via Ollama, em map-reduce.

Corrige os 3 erros do v1:
  1. define num_ctx  -> nada de transcrição truncada em silêncio;
  2. streaming, sem teto de 120 s -> reunião longa também gera ata;
  3. modelo fixo e validado -> não usa "o primeiro do `ollama list`".

Estratégia: resume cada janela de ~10 min (map), depois consolida numa
ata única (reduce). Chamadas pequenas = rápidas e sem estourar contexto.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import OllamaConfig

log = logging.getLogger("ata.summarize")

EMPTY: dict[str, Any] = {
    "summary": "",
    "agenda_items": [],
    "action_items": [],
    "next_steps": "",
    "other_notes": [],
    "participants": [],
    "decisions": [],
    "risks": [],
    "technical_points": {"services": [], "architecture": [], "dependencies": []},
    "cloud_providers": [],
}

_TECHNICAL_POINTS_EMPTY: dict[str, list[str]] = {"services": [], "architecture": [], "dependencies": []}

ProgressCb = Callable[[dict[str, Any]], None]

_MAP_PROMPT = """Você recebe um TRECHO da transcrição de uma reunião (fala rotulada por ME e SYSTEM).
Responda SOMENTE com JSON válido, sem comentários, com as chaves:
- summary: 2 a 4 frases fiéis ao que foi dito neste trecho;
- agenda_items: lista de objetos {{"topic","discussion"}} — um item por assunto tratado neste trecho;
  "discussion" resume os principais pontos e discussões relacionadas a esse tópico (contexto, não
  decisão — decisões vão em "decisions");
- action_items: lista de objetos {{"task","owner","deadline"}}; use null em owner/deadline quando não estiver explícito;
- next_steps: próximos passos combinados neste trecho (string curta; vazio se não houver);
- other_notes: SOMENTE pendências, dúvidas não resolvidas, contradições e outras informações
  relevantes que não caibam em nenhuma das chaves abaixo — riscos e dependências têm chave própria,
  não repita aqui; temas apenas discutidos, sem decisão fechada, também vêm para cá;
- participants: nomes de pessoas com evidência SUFICIENTE de participação na reunião (falaram ou
  foram claramente tratadas como presentes) — nunca "ME"/"SYSTEM", e nunca alguém apenas CITADO de
  passagem por outra pessoa;
- decisions: SOMENTE decisões explicitamente estabelecidas/fechadas neste trecho; cada decisão é uma
  frase que o texto realmente afirma como decidida; sugestão, hipótese, possibilidade em aberto ou
  tema apenas discutido NÃO é decisão (tema discutido vai para other_notes); não acrescente rótulos
  como "Aprovada" ou "Decidido" se a palavra não aparece no texto;
- risks: riscos ou bloqueios com evidência clara neste trecho (não invente a partir de suposição);
- technical_points: objeto {{"services","architecture","dependencies"}}, cada um lista de strings:
  "services" = produtos/serviços/tecnologias citados nominalmente; "architecture" = arquitetura,
  topologia ou padrões técnicos EFETIVAMENTE discutidos (nunca inferidos por conhecimento externo);
  "dependencies" = dependências técnicas ou externas relevantes explicitamente discutidas;
- cloud_providers: provedores de nuvem citados EXPLICITAMENTE pelo nome (AWS/Azure/GCP/OCI/Huawei
  etc.) — não infira o provedor a partir de um serviço específico (ex. não deduza AWS só porque
  "S3" foi citado; só inclua se o provedor em si foi nomeado).
Não invente nada. Listas vazias / string vazia / objeto com listas vazias quando não houver itens.

TRECHO ({header}):
{body}
"""

_REDUCE_PROMPT = """Você recebe uma lista JSON de resumos parciais (em ordem cronológica) de UMA reunião.
Consolide tudo em UMA ata única, em Português do Brasil. Responda SOMENTE com JSON válido com as
mesmas chaves: summary, agenda_items ({{"topic","discussion"}}), action_items ({{"task","owner","deadline"}}),
next_steps, other_notes, participants, decisions, risks,
technical_points ({{"services","architecture","dependencies"}}), cloud_providers.
Regras: agrupe agenda_items pelo mesmo tópico (mescle discussões relacionadas em vez de repetir o
tópico); deduplique itens repetidos em TODAS as listas (action_items, other_notes, participants,
decisions, risks, technical_points.services/architecture/dependencies, cloud_providers — mesmo nome/
texto em janelas diferentes vira uma entrada só); mantenha a ordem cronológica; não invente owner/
deadline (use null); não promova sugestão a decisão — decisions só tem frases que os resumos parciais afirmam como decididas,
sem rótulos como "Aprovada" que não estejam nos resumos, e tema apenas discutido vai para other_notes; não invente participante, risco, arquitetura ou
cloud provider que não estava nos resumos parciais; next_steps deve ser um parágrafo curto
consolidando os combinados; o summary deve ser um resumo executivo de 4 a 8 frases da reunião
inteira; seja conciso, claro e objetivo.

RESUMOS PARCIAIS:
{body}
"""


class OllamaClient:
    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg

    def _open(self, path: str, payload: dict[str, Any] | None, timeout: float):
        """Abre a conexão e devolve a resposta AINDA ABERTA. Quem chama fecha."""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.cfg.base_url.rstrip("/") + path, data=data, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def list_models(self) -> list[dict[str, Any]]:
        try:
            resp = self._open("/api/tags", None, timeout=5)
            try:
                data = json.loads(resp.read().decode("utf-8"))
            finally:
                resp.close()
            return list(data.get("models", []))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise OllamaUnavailable(str(error)) from error

    def model_names(self) -> list[str]:
        return [m.get("name", "") for m in self.list_models() if m.get("name")]

    def generate(self, model: str, prompt: str, *, num_ctx: int, as_json: bool = True) -> str:
        """Consome o streaming linha a linha e devolve o texto acumulado.

        Não usa `with` no urlopen: fechar a resposta antes de ler zera o resultado
        (era o bug que deixava a ata vazia em produção).
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "keep_alive": self.cfg.keep_alive,
            "options": {"temperature": self.cfg.temperature, "num_ctx": num_ctx},
        }
        if as_json:
            payload["format"] = "json"
        chunks: list[str] = []
        try:
            resp = self._open("/api/generate", payload, timeout=self.cfg.request_timeout_seconds)
        except (urllib.error.URLError, OSError) as error:
            raise OllamaUnavailable(str(error)) from error
        try:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise OllamaUnavailable(str(obj["error"]))
                if obj.get("response"):
                    chunks.append(obj["response"])
                if obj.get("done"):
                    break
        except (urllib.error.URLError, OSError) as error:
            raise OllamaUnavailable(str(error)) from error
        finally:
            resp.close()
        return "".join(chunks)

    def warm(self, model: str) -> None:
        try:
            resp = self._open("/api/generate", {"model": model, "prompt": "ok", "stream": False,
                                                "keep_alive": self.cfg.keep_alive}, timeout=120)
            resp.read()
            resp.close()
        except Exception:  # pragma: no cover
            log.debug("warm-up do Ollama falhou (segue o jogo)")


class OllamaUnavailable(RuntimeError):
    pass


# --- helpers puros -----------------------------------------------------------

_EMBED_HINT = ("embed", "bge", "gte", "e5", "nomic-embed", "minilm", "reranker")


def _usable_for_chat(model: dict[str, Any]) -> bool:
    """Descarta modelos de embedding/rerank como candidatos a fallback."""
    name = str(model.get("name", "")).lower()
    family = str((model.get("details") or {}).get("family", "")).lower()
    if any(h in name for h in _EMBED_HINT) or family in ("bert", "nomic-bert"):
        return False
    caps = model.get("capabilities")
    if isinstance(caps, list) and caps and "completion" not in caps and "chat" not in caps:
        return False
    return True


def estimate_tokens(text: str) -> int:
    return int(len(text) / 3.4) + 1


def pick_num_ctx(text: str, cfg: OllamaConfig) -> int:
    want = int(estimate_tokens(text) * 1.35) + 1024
    return max(cfg.num_ctx_min, min(cfg.num_ctx_max, want))


def format_window(segments: list[dict[str, Any]]) -> str:
    """Fala compacta, sem timestamp em ms, mesclando linhas do mesmo falante."""
    lines: list[str] = []
    last_src: str | None = None
    for seg in segments:
        label = "ME" if seg["source"] in ("me", "mic", "microphone") else "SYSTEM"
        text = seg["text"].strip()
        if not text:
            continue
        if label == last_src:
            lines[-1] = lines[-1] + " " + text
        else:
            lines.append(f"{label}: {text}")
            last_src = label
    return "\n".join(lines)


def split_windows(segments: list[dict[str, Any]], window_minutes: float) -> list[list[dict[str, Any]]]:
    if not segments:
        return []
    span = max(1.0, window_minutes * 60.0)
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    edge = span
    for seg in segments:
        while seg["start"] >= edge:
            edge += span
            if current:
                windows.append(current)
                current = []
        current.append(seg)
    if current:
        windows.append(current)
    return windows


def loads_lenient(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    return {}


_FINGERPRINT_LIST_KEYS = ("action_items", "other_notes", "participants", "decisions", "risks",
                          "cloud_providers")
_TECHNICAL_SUBKEYS = ("services", "architecture", "dependencies")


def _merge_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Fallback determinístico quando o reduce do LLM falha."""
    out: dict[str, Any] = {
        "summary": "",
        "agenda_items": [],
        "action_items": [],
        "next_steps": "",
        "other_notes": [],
        "participants": [],
        "decisions": [],
        "risks": [],
        "technical_points": {"services": [], "architecture": [], "dependencies": []},
        "cloud_providers": [],
    }
    seen: dict[str, set] = {k: set() for k in _FINGERPRINT_LIST_KEYS}
    seen_technical: dict[str, set] = {k: set() for k in _TECHNICAL_SUBKEYS}
    summaries: list[str] = []
    next_steps: list[str] = []
    agenda_labels: dict[str, str] = {}
    agenda_discussions: dict[str, list[str]] = {}
    agenda_order: list[str] = []
    for part in partials:
        if part.get("summary"):
            summaries.append(str(part["summary"]).strip())
        if part.get("next_steps"):
            next_steps.append(str(part["next_steps"]).strip())
        for item in part.get("agenda_items", []) or []:
            topic = str((item or {}).get("topic") or "").strip()
            discussion = str((item or {}).get("discussion") or "").strip()
            if not topic:
                continue
            key = topic.lower()
            if key not in agenda_discussions:
                agenda_discussions[key] = []
                agenda_labels[key] = topic
                agenda_order.append(key)
            if discussion:
                agenda_discussions[key].append(discussion)
        for key in _FINGERPRINT_LIST_KEYS:
            for item in part.get(key, []) or []:
                fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
                if fingerprint in seen[key]:
                    continue
                seen[key].add(fingerprint)
                out[key].append(item)
        technical = part.get("technical_points") or {}
        if isinstance(technical, dict):
            for subkey in _TECHNICAL_SUBKEYS:
                for item in technical.get(subkey, []) or []:
                    fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
                    if fingerprint in seen_technical[subkey]:
                        continue
                    seen_technical[subkey].add(fingerprint)
                    out["technical_points"][subkey].append(item)
    out["agenda_items"] = [
        {"topic": agenda_labels[key], "discussion": " ".join(agenda_discussions[key])}
        for key in agenda_order
    ]
    out["summary"] = " ".join(summaries)
    out["next_steps"] = " ".join(next_steps)
    return out


def _normalize_technical_points(raw: Any) -> dict[str, list[str]]:
    """Trata o dict aninhado à parte: o loop genérico de baixo não sabe lidar com
    um dict (cairia no ramo de escalar e o embrulharia numa lista, corrompendo a
    estrutura). Sempre devolve as 3 sub-chaves, mesmo se `raw` vier malformado."""
    out = {k: [] for k in _TECHNICAL_SUBKEYS}
    if not isinstance(raw, dict):
        return out
    for subkey in _TECHNICAL_SUBKEYS:
        value = raw.get(subkey)
        if isinstance(value, list):
            items = value
        elif value:
            items = [value]
        else:
            items = []
        out[subkey] = [str(i).strip() for i in items if str(i).strip()]
    return out


def _empty_payload() -> dict[str, Any]:
    """Cópia independente de EMPTY — `dict(EMPTY)` seria raso e technical_points
    (dict aninhado) ficaria compartilhado entre chamadas/reuniões diferentes."""
    out = dict(EMPTY)
    out["technical_points"] = {k: [] for k in _TECHNICAL_SUBKEYS}
    return out


def _has_content(payload: dict[str, Any]) -> bool:
    """`technical_points` é sempre um dict não-vazio (3 chaves fixas); checar
    `bool(payload["technical_points"])` seria sempre True. Olha as sub-listas."""
    if payload.get("summary"):
        return True
    technical = payload.get("technical_points") or {}
    if any(technical.get(k) for k in _TECHNICAL_SUBKEYS):
        return True
    return any(payload.get(k) for k in EMPTY if k not in ("summary", "technical_points"))

# Rótulos de canal de áudio (ata/obsidian.py) — nunca são pessoas.
_CHANNEL_LABELS = frozenset({"me", "system", "sistema"})
_ITEM_BODY_KEYS = ("risk", "risco", "decision", "decisao", "decisão", "description", "descricao",
                   "descrição", "discussion", "text", "texto", "item", "note", "name")
_ITEM_HEAD_KEYS = ("topic", "topico", "tópico", "title", "titulo", "título")


def is_channel_label(value: Any) -> bool:
    return str(value or "").strip().casefold() in _CHANNEL_LABELS


def clean_owner(value: Any) -> str:
    """Responsável como texto; rótulos de canal (ME/SYSTEM/Sistema) viram vazio,
    inclusive dentro de parênteses: "Ana (ME)" -> "Ana" e
    "(equipe/SYSTEM)" -> "". Nunca infere um nome."""
    text = str(value or "").strip()
    if is_channel_label(text):
        return ""

    def strip_group(m: re.Match[str]) -> str:
        tokens = re.split(r"[\s/,;|+-]+", m.group(1).strip())
        return "" if any(is_channel_label(t) for t in tokens) else m.group(0)

    text = re.sub(r"\(([^()]*)\)", strip_group, text)
    return re.sub(r"\s+", " ", text).strip(" -—/,;")


def item_to_text(item: Any) -> str:
    """Item de lista do LLM (risco, decisão, nota, participante...) como texto.
    String é mantida; dict vira uma frase 'tópico: corpo' com os valores
    textuais conhecidos (ou todos os textuais se nenhuma chave é conhecida)."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        low = {str(k).casefold(): v for k, v in item.items()}

        def pick(keys: tuple[str, ...]) -> list[str]:
            return [t for k in keys if (t := item_to_text(low.get(k)))]

        head, body = pick(_ITEM_HEAD_KEYS)[:1], pick(_ITEM_BODY_KEYS)
        if not head and not body:
            body = [t for v in item.values() if (t := item_to_text(v))]
        if head and body:
            return f"{head[0]}: {' — '.join(body)}"
        return " — ".join(head + body)
    if isinstance(item, (list, tuple)):
        return "; ".join(t for i in item if (t := item_to_text(i)))
    return str(item).strip()


_TEMPLATE_CLOUDS = frozenset({"aws", "azure", "gcp", "oci", "huawei"})


def _dedupe_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".,;:!?…").strip().casefold()


def dedupe_texts(texts: list[str]) -> list[str]:
    """Remove repetidos ignorando maiúsculas, espaços e pontuação final; mantém
    a primeira ocorrência e a ordem."""
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        k = _dedupe_key(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def clean_cloud_providers(providers: list[str]) -> list[str]:
    """Lista igual ao conjunto completo de clouds do prompt/template é eco do
    exemplo, não evidência -> "não identificado" (lista vazia)."""
    unique = dedupe_texts([p for p in providers if p])
    if {_dedupe_key(p) for p in unique} == _TEMPLATE_CLOUDS:
        return []
    return unique


def normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, default in EMPTY.items():
        if key == "technical_points":
            continue
        value = raw.get(key, default)
        if key in ("summary", "next_steps"):
            out[key] = str(value or "").strip()
        elif isinstance(value, list):
            out[key] = value
        elif value:
            out[key] = [value]
        else:
            out[key] = []
    out["technical_points"] = _normalize_technical_points(raw.get("technical_points"))
    fixed_actions: list[dict[str, Any]] = []
    for item in out["action_items"]:
        if isinstance(item, dict):
            fixed_actions.append({
                "task": str(item.get("task") or item.get("text") or "").strip(),
                "owner": (clean_owner(item.get("owner")) or None),
                "deadline": (item.get("deadline") or None),
            })
        elif isinstance(item, str) and item.strip():
            fixed_actions.append({"task": item.strip(), "owner": None, "deadline": None})
    out["action_items"] = [i for i in fixed_actions if i["task"]]

    fixed_agenda: list[dict[str, Any]] = []
    for item in out["agenda_items"]:
        if isinstance(item, dict):
            topic = str(item.get("topic") or "").strip()
            discussion = str(item.get("discussion") or "").strip()
        elif isinstance(item, str):
            topic, discussion = item.strip(), ""
        else:
            continue
        if topic:
            fixed_agenda.append({"topic": topic, "discussion": discussion})
    out["agenda_items"] = fixed_agenda

    for key in ("other_notes", "participants", "decisions", "risks", "cloud_providers"):
        texts = [item_to_text(n) for n in out[key]]
        if key == "participants":
            texts = [t for t in texts if not is_channel_label(t)]
        texts = dedupe_texts([t for t in texts if t])
        out[key] = clean_cloud_providers(texts) if key == "cloud_providers" else texts
    return out


def summarize_meeting(
    segments: list[dict[str, Any]],
    cfg: OllamaConfig,
    *,
    client: OllamaClient | None = None,
    model_override: str | None = None,
    progress: ProgressCb | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = client or OllamaClient(cfg)
    emit = progress or (lambda _e: None)
    diag: dict[str, Any] = {"status": "ok", "steps": []}

    if not segments:
        diag.update(status="skipped", reason="reunião sem transcrição")
        return _empty_payload(), diag

    try:
        catalog = client.list_models()
    except OllamaUnavailable as error:
        diag.update(status="failed", reason=f"Ollama indisponível: {error}")
        return _empty_payload(), diag
    available = [m.get("name", "") for m in catalog if m.get("name")]
    if not available:
        diag.update(status="failed", reason="nenhum modelo instalado no Ollama")
        return _empty_payload(), diag

    model = model_override or cfg.model
    if model not in available:
        if not cfg.allow_fallback:
            diag.update(status="failed", reason=f"modelo '{model}' não está instalado no Ollama")
            return _empty_payload(), diag
        candidates = [m["name"] for m in catalog if _usable_for_chat(m)] or available
        model = candidates[0]
        diag["model_fallback"] = {"requested": model_override or cfg.model, "used": model}
        diag["steps"].append(f"modelo '{model_override or cfg.model}' ausente; usando '{model}'")

    windows = split_windows(segments, cfg.window_minutes)
    emit({"type": "summary_progress", "stage": "map", "total": len(windows), "done": 0})
    partials: list[dict[str, Any]] = []
    map_error: str | None = None
    for idx, window in enumerate(windows, start=1):
        body = format_window(window)
        header = f"minuto {int(window[0]['start'] // 60)}–{int(window[-1]['end'] // 60)}"
        prompt = _MAP_PROMPT.format(header=header, body=body)
        try:
            raw = client.generate(model, prompt, num_ctx=pick_num_ctx(prompt, cfg))
        except OllamaUnavailable as error:
            map_error = f"janela {idx}/{len(windows)}: {error}"
            break
        partials.append(normalize_payload(loads_lenient(raw)))
        emit({"type": "summary_progress", "stage": "map", "total": len(windows), "done": idx})

    covered = len(partials)
    if covered == 0:
        diag.update(status="failed", reason=f"nenhuma janela resumida ({map_error})")
        return _empty_payload(), diag

    if covered == 1:
        final = partials[0]
    else:
        reduce_body = json.dumps(partials, ensure_ascii=False)
        prompt = _REDUCE_PROMPT.format(body=reduce_body)
        emit({"type": "summary_progress", "stage": "reduce"})
        try:
            raw = client.generate(model, prompt, num_ctx=pick_num_ctx(prompt, cfg))
            final = normalize_payload(loads_lenient(raw))
            if not _has_content(final):
                raise OllamaUnavailable("reduce vazio")
            diag["steps"].append("reduce via LLM")
        except OllamaUnavailable as error:
            diag["steps"].append(f"reduce do LLM falhou ({error}); consolidação determinística")
            final = _merge_partials(partials)

    diag["model"] = model
    diag["windows"] = len(windows)
    diag["covered_windows"] = covered
    has_content = _has_content(final)
    if covered < len(windows):
        diag["status"] = "partial"
        diag["reason"] = (f"a ata cobre {covered}/{len(windows)} janelas — "
                          f"o restante da reunião ficou de fora ({map_error})")
    elif not has_content:
        diag["status"] = "empty"
        diag["reason"] = "Ollama respondeu sem conteúdo aproveitável"
    else:
        diag["status"] = "ok"
    return final, diag
