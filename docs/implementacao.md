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

- `safe >= 0.85` e `irreversible < 0.40`: allow
- `safe <= 0.20` e `irreversible >= 0.70`: block
- faixa do meio em `terminal`/`execute_code`: block neste desenho, porque oneshot não tem humano para o `approve`
- faixa do meio em edição de arquivo: allow
- API fora: block

`read_file` não chama a API.

## Fora do prompt de fronteira

`route_model` e `classify` rodam uma vez por sessão e vão para o ledger com `applied: false`. Stuck só depois de 4 resultados de tool, uma vez por sessão, em `p >= 0.75`. O texto injetado vai na mensagem do usuário via `pre_llm_call`, nunca no system prompt.

`pre_verify` chama o verificador uma vez. Continua se a confiança de "feito" fica abaixo de 0.45. Acima disso, aceita e não abre outro turno.

## Compactação

`context.engine = jev-decision`. O engine pontua resultados de tool no request. Abaixo de `KEEP_MIN` (0.30), com pelo menos 4 candidatos, o texto vira o stub `{"jev":"dropped","reason":"compaction"}`. Os dois últimos resultados ficam. O que sobrevive não é resumido.

O catálogo de tools do turno não é filtrado. O Hermes congela `tools` na sessão.
