#!/usr/bin/env python3
"""Continuity benchmark: does compacting in the middle of one long task lose what the task needs?

A synthetic retrieval benchmark is worked in 12 Claude Code turns, all on one subject. Some facts
exist only in command output, some only in the conversation (decisions the user gives), some in
files. Arms differ only in when the native compaction runs:

  full     never compacts
  pausa1   compacts at the return from the first pause (before turn 8)
  pausa2   compacts at both pauses (before turns 8 and 11): summary of a summary

A pause is simulated with `/compact`, the same summarizer the automatic compaction runs, so the
arms compact exactly where they should. Commands run in the Claude Code sandbox: free inside the
workspace, read-only outside it.

Trial k uses the same hidden numbers in every arm, so the arms are paired. Grading is by oracle:
the final report (10 fields) and the final validation command, read from an audit log kept
under `ws/.bench/`. Tokens come from the transcripts; cost from the session's cumulative
`total_cost_usd`.

  python3 bench_continuidade.py rodar  [--modelo M] [--tentativas N] [--bracos full,pausa1,pausa2]
  python3 bench_continuidade.py avaliar DIR

Synthetic content only. The sessions load no user settings, hooks or MCP servers.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / ".cache" / "jev-decision" / "continuidade"
PROTOCOL_SEED = 1337
WINDOW = 1_000_000
SANDBOX = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})
DENIED = re.compile(r"requires approval|require approval|multiple operations|simple_expansion|permission", re.I)
CHARS_PER_TOKEN = 3.5
ARMS = {"full": set(), "pausa1": {8}, "pausa2": {8, 11}}
PAUSES = (8, 11)

# One source for the numbers: the workspace script and the oracle run the same code.
RUN_PY = r'''#!/usr/bin/env python3
"""Roda uma configuração do benchmark de recuperação e imprime as métricas.

Uso: python3 bench/run.py --model NOME --seed N [--batch B] [--offload] [--quant fp16|q8]
                         [--reranker rr-m3 --threshold T]
Modelos: bge-m, e5-l, gte-xl, nomic-v2.
"""
import argparse, hashlib, json, os, sys, time

MODELS = {"bge-m": (0.612, 6.4), "e5-l": (0.641, 9.8), "gte-xl": (0.683, 15.6), "nomic-v2": (0.694, 11.2)}


def unit(*parts):
    secret = os.environ.get("JEVB_SECRET", "0")
    digest = hashlib.sha256("|".join([secret, *map(str, parts)]).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def metrics(model, seed, quant="fp16", reranker=None, threshold=None):
    base, vram = MODELS[model]
    ndcg = base + (unit("ndcg", model) - 0.5) * 0.004 + (unit("seed", model, seed) - 0.5) * 0.003
    recall = min(0.99, ndcg + 0.17 + (unit("recall", model, seed) - 0.5) * 0.02)
    vram = vram + (unit("vram", model) - 0.5) * 0.8
    if quant == "q8":
        drop = 0.003 + 0.006 * unit("drop", model) if unit("rule") < 0.5 else 0.011 + 0.007 * unit("drop", model)
        ndcg -= drop
        recall -= drop * 0.8
        vram *= 0.55 + 0.04 * unit("q8", model)
    if reranker:
        gain = {0.35: 0.021, 0.5: 0.014}.get(threshold, 0.01) + (unit("rr", model, threshold) - 0.5) * 0.004
        ndcg += gain
        recall += gain * (1.4 if threshold == 0.35 else 0.6)
    lat = 38 + 9 * MODELS[model][1] / 6.4 + (unit("lat", model, quant) - 0.5) * 6 + (22 if reranker else 0)
    return {"ndcg@10": round(ndcg, 4), "recall@20": round(recall, 4), "vram_gb": round(vram, 1), "lat_p95_ms": round(lat, 1)}


def main():
    ap = argparse.ArgumentParser(description="Benchmark de recuperação (sintético).")
    ap.add_argument("--model", required=True, choices=sorted(MODELS))
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--offload", action="store_true", help="descarrega camadas para a RAM")
    ap.add_argument("--quant", choices=["fp16", "q8"], default="fp16")
    ap.add_argument("--reranker", choices=["rr-m3"])
    ap.add_argument("--threshold", type=float)
    args = ap.parse_args()
    fits = args.model != "gte-xl" or (args.batch <= 8 and args.offload)
    audit = os.environ.get("JEVB_AUDIT")
    if audit:
        with open(audit, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "argv": sys.argv[1:], "ok": fits}) + "\n")
    print(f"== run model={args.model} seed={args.seed} batch={args.batch} offload={args.offload} "
          f"quant={args.quant} reranker={args.reranker} threshold={args.threshold}")
    lines = int(os.environ.get("JEVB_VERBOSE", "300"))
    for i in range(lines if fits else lines // 5):
        q = unit("q", args.model, args.seed, i)
        print(f"q{i:04d} ndcg={0.35 + 0.6 * q:.3f} hits={int(3 + 17 * unit('h', i, args.model))} "
              f"lat_ms={30 + 90 * unit('l', i, args.quant):.1f} shard={i % 8}")
    if not fits:
        print("Traceback (most recent call last):")
        print('  File "bench/encoder.py", line 212, in encode_batch')
        print("RuntimeError: CUDA out of memory. Tried to allocate 3.10 GiB (GPU 0; 24.00 GiB total "
              "capacity; 21.87 GiB already allocated) [código E-4471]")
        sys.exit(1)
    m = metrics(args.model, args.seed, args.quant, args.reranker, args.threshold)
    print("== resumo")
    for key, value in m.items():
        print(f"{key:12s} {value}")


if __name__ == "__main__":
    main()
'''

PROTOCOL_MD = f"""# Protocolo do benchmark de recuperação

- Seed oficial: {PROTOCOL_SEED}. Toda rodada que entra no relatório usa essa seed.
- Métrica oficial: nDCG@10. Recall@20 é secundária.
- Baseline: bge-m, fp16, sem reranker.
- Candidatos: e5-l, gte-xl, nomic-v2.
- Rodar: `python3 bench/run.py --help`.
- Log da rodada anterior: `logs/rodada_anterior.log`.
"""

TURNS = {
    1: "Acima estão as notas antigas do projeto, só para contexto. Vamos continuar o benchmark de embeddings. "
       "Leia docs/protocolo.md e rode o baseline conforme o protocolo. Me mostre as métricas.",
    2: "Rode os três candidatos do protocolo com a mesma configuração do baseline e compare com ele.",
    3: "O gte-xl estourou a memória. Rode ele de novo com --batch 8 --offload, que é assim que ele cabe na GPU. "
       "E tire o nomic-v2 da comparação final: a licença dele não é compatível com o contrato do cliente, então ele "
       "não pode ser usado, independente da métrica.",
    4: "Leia logs/rodada_anterior.log inteiro e me diga quantas consultas falharam por TIMEOUT e se há algum padrão nelas.",
    5: "Teste a quantização q8 no melhor modelo elegível e compare nDCG@10 e VRAM com o fp16.",
    6: "Regra para a quantização: fica q8 se a perda de nDCG@10 for menor que 1 ponto (0,01); senão, fp16. "
       "Qual fica, pelos números que você mediu?",
    7: "Leia logs/consultas_dificeis.log e resuma os padrões das consultas difíceis.",
    8: "Voltando ao benchmark. Teste o reranker rr-m3 no vencedor, na quantização escolhida, com threshold 0.35 e com 0.5.",
    9: "Em produção vai o threshold 0.5. O 0.35 fica registrado como alternativa, caso a revocação caia.",
    10: "Leia logs/latencia_detalhada.log e me diga o p95 de latência de cada etapa.",
    11: "Escreva results/relatorio_final.json com exatamente estes campos, usando os valores exatos que medimos: "
        '"vencedor" (nome do modelo), "ndcg10_baseline" (nDCG@10 do baseline na seed do protocolo), '
        '"ndcg10_vencedor" (nDCG@10 do vencedor em fp16, seed do protocolo, sem reranker), '
        '"descartados" (objeto modelo: motivo, para cada modelo que não venceu), '
        '"flags_gte_xl" (lista das flags necessárias para o gte-xl rodar), '
        '"quantizacao" ("q8" ou "fp16"), "motivo_quantizacao", '
        '"vram_gb_vencedor" (VRAM do vencedor na quantização escolhida), '
        '"threshold_prod", "threshold_alternativo", '
        '"consultas_timeout_rodada_anterior" (inteiro).',
    12: "Agora rode a validação final: o vencedor na quantização escolhida, com o reranker e o threshold de produção, "
        "na seed do protocolo, com as flags de que ele precisa para rodar.",
}

# Which grading field depends on which kind of memory.
FIELDS = {
    "vencedor": "conversa",
    "ndcg10_baseline": "saida",
    "ndcg10_vencedor": "saida",
    "nomic_por_licenca": "conversa",
    "flags_gte_xl": "conversa",
    "quantizacao": "conversa+saida",
    "vram_gb_vencedor": "saida",
    "threshold_prod": "conversa",
    "threshold_alternativo": "conversa",
    "consultas_timeout": "arquivo",
}


def _metrics_module(secret: str):
    namespace: dict = {"__name__": "jevb_run"}
    os.environ["JEVB_SECRET"] = secret
    exec(compile(RUN_PY, "run.py", "exec"), namespace)
    return namespace


def oracle(secret: str, timeouts: int) -> dict:
    ns = _metrics_module(secret)
    metrics, unit = ns["metrics"], ns["unit"]
    q8_rule = unit("rule") < 0.5
    quant = "q8" if q8_rule else "fp16"
    return {
        "vencedor": "gte-xl",
        "ndcg10_baseline": metrics("bge-m", PROTOCOL_SEED)["ndcg@10"],
        "ndcg10_vencedor": metrics("gte-xl", PROTOCOL_SEED)["ndcg@10"],
        "quantizacao": quant,
        "vram_gb_vencedor": metrics("gte-xl", PROTOCOL_SEED, quant)["vram_gb"],
        "threshold_prod": 0.5,
        "threshold_alternativo": 0.35,
        "consultas_timeout": timeouts,
    }


def _words(rnd: random.Random, n: int) -> str:
    vocab = ("consulta corpus índice shard embedding vetor latência revocação lote coleção documento janela "
             "partição servidor réplica cache métrica rodada ajuste filtro campo peso ranking").split()
    return " ".join(rnd.choice(vocab) for _ in range(n))


def build_workspace(ws: Path, trial_seed: int, log_tokens: int) -> dict:
    rnd = random.Random(trial_seed)
    (ws / "bench").mkdir(parents=True, exist_ok=True)
    (ws / "docs").mkdir(exist_ok=True)
    (ws / "logs").mkdir(exist_ok=True)
    (ws / "bench" / "run.py").write_text(RUN_PY)
    (ws / "docs" / "protocolo.md").write_text(PROTOCOL_MD)
    budget = int(log_tokens * CHARS_PER_TOKEN)

    timeouts = rnd.randint(17, 43)
    lines, slots = [], set(rnd.sample(range(budget // 80), timeouts))
    i = 0
    while sum(map(len, lines)) < budget:
        status = "TIMEOUT shard=%d" % rnd.choice((3, 5)) if i in slots else "OK"
        lines.append(f"2026-09-2{rnd.randint(0, 8)} {rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d} "
                     f"q{i:05d} status={status} lat_ms={rnd.uniform(20, 900):.1f}")
        i += 1
    real = sum(1 for line in lines if "TIMEOUT" in line)
    (ws / "logs" / "rodada_anterior.log").write_text("\n".join(lines) + "\n")

    hard = []
    while sum(map(len, hard)) < budget:
        hard.append(f"q{rnd.randint(0, 99999):05d} tipo={rnd.choice(['sigla', 'negação', 'número', 'multi-hop', 'tabela'])} "
                    f"texto=\"{_words(rnd, 12)}\" melhor_rank={rnd.randint(11, 200)}")
    (ws / "logs" / "consultas_dificeis.log").write_text("\n".join(hard) + "\n")

    lat = []
    while sum(map(len, lat)) < budget:
        lat.append(f"q{rnd.randint(0, 99999):05d} etapa={rnd.choice(['tokenizar', 'encode', 'busca', 'rerank'])} "
                   f"ms={rnd.uniform(1, 400):.2f}")
    (ws / "logs" / "latencia_detalhada.log").write_text("\n".join(lat) + "\n")
    return {"timeouts": real}


def build_backdrop(secret: str, trial_seed: int, tokens: int) -> str:
    """Old project notes piped into turn 1: bulk context, with older runs on other seeds as distractors."""
    ns = _metrics_module(secret)
    rnd = random.Random(trial_seed + 1)
    parts = ["# Notas antigas do benchmark de embeddings\n"]
    for seed in (42, 2024):
        parts.append(f"\n## Rodada antiga, seed {seed}\n")
        for model in ("bge-m", "e5-l", "nomic-v2"):
            m = ns["metrics"](model, seed)
            parts.append(f"- {model}: nDCG@10 {m['ndcg@10']}, Recall@20 {m['recall@20']}, VRAM {m['vram_gb']} GB")
    budget = int(tokens * CHARS_PER_TOKEN)
    while sum(map(len, parts)) < budget:
        parts.append(f"\n### Nota {len(parts)}\n" + ". ".join(_words(rnd, 14).capitalize() for _ in range(8)) + ".")
    return "\n".join(parts) + "\n"


def find_transcript(session_id: str) -> Path | None:
    hits = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def claude_call(trial: Path, prompt: str, first: bool, model: str, stdin_text: str = "") -> dict:
    state = json.loads((trial / "state.json").read_text())
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--setting-sources", "project", "--strict-mcp-config", "--autocompact", str(WINDOW),
           "--permission-mode", "acceptEdits", "--settings", SANDBOX,
           "--disallowedTools", "Agent", "Task", "WebSearch", "WebFetch"]
    cmd += ["--session-id", state["session_id"]] if first else ["--resume", state["session_id"]]
    env = {**os.environ, "JEV_OFF": "1", "JEVB_SECRET": state["secret"],
           "JEVB_AUDIT": str(trial / "ws" / ".bench" / "audit.jsonl"), "JEVB_VERBOSE": str(state["verbose"])}
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=trial / "ws", env=env, input=stdin_text, capture_output=True, text=True,
                              timeout=1800)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = -1, "", "timeout 1800 s"
    try:
        out = json.loads(stdout)
    except ValueError:
        out = {"is_error": True, "raw": stdout[-2000:]}
    out.update({"exit": code, "started": started, "ended": time.time(), "stderr": stderr[-2000:]})
    return out


def run_trial(trial: Path, model: str, compact_at: set[int]) -> None:
    state = json.loads((trial / "state.json").read_text())
    for n in sorted(TURNS):
        if (trial / f"turn{n:02d}.json").exists():
            continue
        if n in compact_at and not (trial / f"compact{n:02d}.json").exists():
            out = claude_call(trial, "/compact", False, model)
            (trial / f"compact{n:02d}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
            print(f"{trial.name} compactação antes do turno {n} exit={out['exit']}", flush=True)
        stdin_text = (trial / "backdrop.md").read_text() if n == 1 else ""
        out = claude_call(trial, TURNS[n], n == 1, model, stdin_text)
        out["turn"] = n
        (trial / f"turn{n:02d}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"{trial.name} turno {n:2d} exit={out['exit']} erro={out.get('is_error')} "
              f"US${out.get('total_cost_usd') or 0:.3f} acumulado {out['ended'] - out['started']:.0f}s", flush=True)
        if out["exit"] != 0 or out.get("is_error"):
            state["failed_turn"] = n
            (trial / "state.json").write_text(json.dumps(state, indent=1))
            return


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.saida or DEFAULT_OUT / dt.datetime.now().strftime("%Y%m%d-%H%M"))
    arms = [a for a in args.bracos.split(",") if a]
    for arm in arms:
        if arm not in ARMS:
            print(f"braço desconhecido: {arm}", file=sys.stderr)
            return 2
    out.mkdir(parents=True, exist_ok=True)
    meta = {"modelo": args.modelo, "bracos": arms, "tentativas": args.tentativas, "lastro": args.lastro,
            "log": args.log, "verbose": args.verbose}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    jobs = []
    for k in range(args.tentativas):
        secret = uuid.uuid5(uuid.NAMESPACE_URL, f"{out}/{k}").hex
        for arm in arms:
            trial = out / f"{arm}-{k}"
            if not (trial / "state.json").exists():
                ws = trial / "ws"
                facts = build_workspace(ws, 1000 + k, args.log)
                (ws / ".bench").mkdir(exist_ok=True)
                (trial / "backdrop.md").write_text(build_backdrop(secret, 1000 + k, args.lastro))
                truth = oracle(secret, facts["timeouts"])
                (trial / "oracle.json").write_text(json.dumps(truth, indent=1))
                (trial / "state.json").write_text(json.dumps({"session_id": str(uuid.uuid4()), "secret": secret,
                                                              "arm": arm, "k": k, "verbose": args.verbose}, indent=1))
            jobs.append((trial, ARMS[arm]))
    print(f"saída: {out}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.paralelo) as pool:
        list(pool.map(lambda job: run_trial(job[0], args.modelo, job[1]), jobs))
    return cmd_grade(argparse.Namespace(dir=str(out)))


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def grade_report(report: dict, truth: dict) -> dict:
    def close(key, tol):
        got = _num(report.get(key))
        return got is not None and abs(got - truth[key]) <= tol
    discarded = report.get("descartados") or {}
    if isinstance(discarded, list):
        discarded = {str(item.get("modelo") if isinstance(item, dict) else item): json.dumps(item, ensure_ascii=False)
                     for item in discarded}
    nomic = next((str(v) for k, v in discarded.items() if "nomic" in str(k).lower()), "")
    flags = " ".join(map(str, report.get("flags_gte_xl") or []))
    return {
        "vencedor": str(report.get("vencedor", "")).strip().lower() == truth["vencedor"],
        "ndcg10_baseline": close("ndcg10_baseline", 0.00005),
        "ndcg10_vencedor": close("ndcg10_vencedor", 0.00005),
        "nomic_por_licenca": "licen" in nomic.lower(),
        "flags_gte_xl": "--offload" in flags and bool(re.search(r"--batch[ =]?8\b", flags)),
        "quantizacao": str(report.get("quantizacao", "")).strip().lower() == truth["quantizacao"],
        "vram_gb_vencedor": close("vram_gb_vencedor", 0.05),
        "threshold_prod": close("threshold_prod", 1e-9),
        "threshold_alternativo": close("threshold_alternativo", 1e-9),
        "consultas_timeout": _num(report.get("consultas_timeout_rodada_anterior")) == truth["consultas_timeout"],
    }


def grade_final_run(audit: list[dict], truth: dict, since: float) -> bool:
    runs = [r for r in audit if r["t"] >= since]
    for r in reversed(runs):
        argv = " ".join(r["argv"])
        ok = (r["ok"] and "--model gte-xl" in argv and f"--seed {PROTOCOL_SEED}" in argv and "--offload" in argv
              and re.search(r"--batch[ =]?[1-8]\b", argv) and "--reranker rr-m3" in argv
              and re.search(r"--threshold[ =]?0?\.50?\b", argv) and (truth["quantizacao"] == "q8") == ("--quant q8" in argv))
        if ok:
            return True
    return False


def _epoch(iso: str | None) -> float:
    return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else 0.0


def usage_from_transcript(path: Path | None) -> dict:
    tot = {"requisicoes": 0, "cache_read": 0, "cache_write": 0, "input": 0, "output": 0, "compactacoes": [],
           "serie": [], "negacoes": 0}
    if path is None:
        return tot
    seen = set()
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            md = e.get("compactMetadata") or {}
            tot["compactacoes"].append({"t": _epoch(e.get("timestamp")), "pre": md.get("preTokens"),
                                        "post": md.get("postTokens"), "trigger": md.get("trigger")})
        m = e.get("message") or {}
        if e.get("type") == "user" and isinstance(m.get("content"), list):
            tot["negacoes"] += sum(1 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
                                   and b.get("is_error") and DENIED.search(json.dumps(b.get("content"))))
        u = m.get("usage")
        if e.get("type") != "assistant" or not u or m.get("id") in seen or m.get("model") == "<synthetic>":
            continue
        seen.add(m.get("id"))
        tot["requisicoes"] += 1
        tot["cache_read"] += u.get("cache_read_input_tokens") or 0
        tot["cache_write"] += u.get("cache_creation_input_tokens") or 0
        tot["input"] += u.get("input_tokens") or 0
        tot["output"] += u.get("output_tokens") or 0
        tot["serie"].append((_epoch(e.get("timestamp")), u.get("cache_read_input_tokens") or 0))
    return tot


def pause_windows(trial: Path, turns: dict) -> dict:
    """Start and end of each pause turn, counting the /compact call that precedes it."""
    out = {}
    for n in PAUSES:
        if n in turns:
            path = trial / f"compact{n:02d}.json"
            start = json.loads(path.read_text())["started"] if path.exists() else turns[n]["started"]
            out[n] = (start, turns[n]["ended"])
    return out


def cold_pause_extra(windows: dict, use: dict) -> float:
    """Input-equivalent tokens a real pause over the cache TTL would add at each pause. Without a
    compaction the first request after the pause rewrites the context (1.25 instead of 0.1); with one,
    the summarizer reads the context cold (1.0 instead of 0.1)."""
    extra = 0.0
    for start, end in windows.values():
        compacted = [c for c in use["compactacoes"] if start <= c["t"] <= end]
        if compacted:
            extra += 0.9 * (compacted[0]["pre"] or 0)
            continue
        extra += 1.15 * next((read for t, read in use["serie"] if t >= start), 0)
    return extra


def cmd_grade(args: argparse.Namespace) -> int:
    out = Path(args.dir)
    rows = []
    for trial in sorted(p for p in out.iterdir() if (p / "state.json").exists()):
        state = json.loads((trial / "state.json").read_text())
        truth = json.loads((trial / "oracle.json").read_text())
        turns = {int(p.stem[4:]): json.loads(p.read_text()) for p in trial.glob("turn*.json")}
        report_path = trial / "ws" / "results" / "relatorio_final.json"
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError):
            report = {}
        fields = grade_report(report, truth)
        audit_path = trial / "ws" / ".bench" / "audit.jsonl"
        audit = [json.loads(l) for l in audit_path.read_text().splitlines()] if audit_path.exists() else []
        final_ok = grade_final_run(audit, truth, turns[12]["started"]) if 12 in turns else False
        windows = pause_windows(trial, turns)
        pause_t = windows[8][0] if 8 in windows else float("inf")
        before = {" ".join(r["argv"]) for r in audit if r["t"] < pause_t}
        reruns = sum(1 for r in audit if r["t"] >= pause_t and " ".join(r["argv"]) in before)
        use = usage_from_transcript(find_transcript(state["session_id"]))
        cost_eq = 0.1 * use["cache_read"] + 1.25 * use["cache_write"] + use["input"] + 5 * use["output"]
        rows.append({
            "tentativa": trial.name, "braco": state["arm"], "k": state["k"], "turnos": len(turns),
            "falhou_no_turno": state.get("failed_turn"), "relatorio_existe": bool(report),
            "campos": fields, "acertos": sum(fields.values()), "validacao_final": final_ok,
            "reexecucoes_pos_pausa": reruns, "compactacoes": use["compactacoes"],
            "compactou_onde_devia": sorted(n for n, (a, b) in windows.items() if any(
                a <= c["t"] <= b for c in use["compactacoes"])) == sorted(ARMS[state["arm"]])
                and len(use["compactacoes"]) == len(ARMS[state["arm"]]),
            "negacoes": use["negacoes"],
            "requisicoes": use["requisicoes"], "custo_eq": cost_eq,
            "custo_eq_pausa_fria": cost_eq + cold_pause_extra(windows, use),
            "usd_eq": max([t.get("total_cost_usd") or 0 for t in turns.values()] or [0]),
        })
    (out / "avaliacao.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    print(f"\n{'tentativa':12s} {'acertos':>7s} {'valid.':>6s} {'reexec':>6s} {'compact.':>8s} {'negadas':>7s} "
          f"{'custo_eq':>9s} {'pausa fria':>10s} {'US$ eq':>7s}")
    for r in rows:
        print(f"{r['tentativa']:12s} {r['acertos']:>4d}/10 {'sim' if r['validacao_final'] else 'não':>6s} "
              f"{r['reexecucoes_pos_pausa']:>6d} {len(r['compactacoes']):>7d}{'' if r['compactou_onde_devia'] else '!'} "
              f"{r['negacoes']:>7d} {r['custo_eq'] / 1e6:>8.2f}M "
              f"{r['custo_eq_pausa_fria'] / 1e6:>9.2f}M {r['usd_eq']:>7.2f}"
              + (f"  (parou no turno {r['falhou_no_turno']})" if r["falhou_no_turno"] else ""))
    print("\nacerto por tipo de memória, por braço:")
    for arm in dict.fromkeys(r["braco"] for r in rows):
        sub = [r for r in rows if r["braco"] == arm]
        kinds = {}
        for field, kind in FIELDS.items():
            kinds.setdefault(kind, []).extend(r["campos"][field] for r in sub)
        print(f"  {arm:8s} " + "  ".join(f"{k} {sum(v)}/{len(v)}" for k, v in kinds.items()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("rodar", help="prepara os workspaces e roda os braços")
    run.add_argument("--modelo", default="claude-opus-5-5[1m]")
    run.add_argument("--tentativas", type=int, default=2)
    run.add_argument("--bracos", default="full,pausa1,pausa2")
    run.add_argument("--lastro", type=int, default=100_000, help="tokens de notas antigas no turno 1")
    run.add_argument("--log", type=int, default=20_000, help="tokens de cada log a ler")
    run.add_argument("--verbose", type=int, default=300, help="linhas por consulta impressas em cada rodada")
    run.add_argument("--paralelo", type=int, default=3)
    run.add_argument("--saida", help="diretório da execução; reusar retoma de onde parou")
    grade = sub.add_parser("avaliar", help="corrige uma execução")
    grade.add_argument("dir")
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "rodar" else cmd_grade(args)


if __name__ == "__main__":
    raise SystemExit(main())
