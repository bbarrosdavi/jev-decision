# Implementação

Origem: [N01ennn, 25 set 2026](https://x.com/N01ennn/status/2103542021071978601). A tese do post é tirar do modelo de fronteira os forks que não produzem texto guardado: gate de tool, escolha de modelo, relevância de contexto, fim de tarefa, ciclo preso, roteamento. A decisão fica fora da thread principal para não invalidar o prefix cache.

## Arquivos

| Arquivo | Papel |
|---|---|
| `plugin.yaml` | Nome `jev-decision`. Exige `TYPESAFE_API_KEY`. |
| `__init__.py` | `register`: três hooks e `JevContextEngine`. |
| `client.py` | HTTP stdlib para `api.typesafe.ai`. Sem SDK no venv compartilhado. |
| `policy.py` | Classe da chamada e composição do gate. Sem rede. |
| `forks.py` | `tool_gate`, `stuck`, `completion`, `route_model`, `classify`. |
| `engine.py` | Compactação no request. |
| `ledger.py` | Append em `logs/jev-decisions.jsonl`, com redação. |
| `test_policy.py` | Política e redação, sem rede. |
| `bench.py` | Latência e tokens de chamadas isoladas. |

## Gate

Tools gated: `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`.

`classify` devolve `skip`, `irreversible` ou `gate`.

`irreversible` por regex: `git checkout/restore/reset/clean`, `rm -rf`, `sudo`, `mkfs`/`shutdown`, `curl|sh`, `dd if=`, `chmod -R 777`, `push --force`, `drop table`, `truncate table`, fork bomb. Também path de credencial (`.env`, chaves, `auth.json`) e `write_file`/`patch` fora do cwd.

`compose_gate`:

- regra de código irreversível: `approve`, sem chamada de API
- `costs_money` ou `externally_visible` >= 0.70: `approve`
- `safe >= 0.85` e `irreversible < 0.40`: allow
- `safe <= 0.20` e `irreversible >= 0.70`: block
- faixa do meio em `terminal`/`execute_code`: `approve`
- faixa do meio em edição de arquivo: allow
- API fora: block

O estado do gate inclui até 3 trechos de arquivo citados no comando, só dentro do cwd, nunca path de credencial.

## Completion

`decide_done` exige confiança >= 0.5, quality >= 1.2, grounded >= 0.45, e os `changed_paths` existentes no disco. Confiança abaixo de 0.5 não aceita. Arquivo citado e ausente não aceita. A checagem de existência é código. O Jev não confirma sozinho que o arquivo foi gravado.

## Dispatch

`delegate_task` que passou no gate chama `dispatch_worker`. Menu `review` / `implement` / `research`, ou `review` / `billing` se o classify da sessão foi `billing`. Confiança abaixo da barra (0.85, sobe 0.05 se as últimas avaliações tiverem grounded médio < 0.40) força `review` via `action: modify` nos args. Não entra no prompt de fronteira.

## Compactação

Noul por resultado de tool e por bloco de reasoning. `read_file` usa a pergunta de retrieval (`answers goal`). Banda: abaixo de 0.30 stub, 0.30–0.55 os primeiros 240 caracteres do original, 0.55–0.80 os primeiros 800, acima disso o texto inteiro. Não há paráfrase. Os dois últimos resultados de tool não entram na pontuação.

## O que não entra no loop

O router grava `fast`/`strong` e não troca o modelo. Trocar no meio da thread reconstrói o cache. O catálogo de tools fica congelado pela mesma razão. Judge de trace roda em `on_session_end` e só ajusta a barra do dispatch.

`read_file` não chama a API.

## Fora do prompt de fronteira

`route_model` e `classify` rodam uma vez por sessão e vão para o ledger com `applied: false`. Stuck só depois de 4 resultados de tool, uma vez por sessão, em `p >= 0.75`. O texto injetado vai na mensagem do usuário via `pre_llm_call`, nunca no system prompt.

`pre_verify` chama o verificador uma vez. Continua se a confiança de "feito" fica abaixo de 0.45. Acima disso, aceita e não abre outro turno.

## Compactação

`context.engine = jev-decision`. O engine pontua resultados de tool no request. Abaixo de `KEEP_MIN` (0.30), com pelo menos 4 candidatos, o texto vira o stub `{"jev":"dropped","reason":"compaction"}`. Os dois últimos resultados ficam. O que sobrevive não é resumido.

O catálogo de tools do turno não é filtrado. O Hermes congela `tools` na sessão.
