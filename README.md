# hermes-jev-decision

Camada de decisão TypeSafe Jev ao lado do loop do [Hermes Agent](https://github.com/nousresearch/hermes-agent). O modelo de fronteira continua gerando o texto. O Jev só classifica.

Post de origem: [Find every fork your agent pays frontier prices for](https://x.com/N01ennn/status/2103542021071978601).

Este plugin não é o `jev-typesafe` do catálogo. Aquele expõe tools (`jev_check`, `jev_route`, `jev_score`, `jev_evaluate`) que o modelo precisa chamar. Este registra hooks. O modelo não chama o Jev.

## O que entra no loop

| Hook | Função |
|---|---|
| `pre_tool_call` | Gate de `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`. O resto não chama a API. |
| `transform_tool_result` | Stuck a cada chamada de tool. O aviso vai no resultado mais novo. |
| `post_tool_call` | Observa corridas de teste para o verificador. |
| `pre_llm_call` | Router e classify numa chamada, no primeiro turno. Troca de assunto, uma vez por pedido. |
| `pre_verify` | Continua só com evidência: teste falhou, path ausente, ou o Jev confiante de que falta algo. |
| context engine | O compressor nativo, com um gatilho: compacta cedo quando o pedido novo abre outra tarefa. |

O router não troca o modelo do chat. Não há hook que faça isso sem invalidar o prefix cache.

## Política

Limiares em `policy.py`: `SAFE_ALLOW` 0.85, `IRREV_ALLOW_MAX` 0.40, `SAFE_BLOCK` 0.20, `IRREV_BLOCK` 0.70, `STUCK_FIRE` 0.75, `DONE_CONTINUE` 0.45.

Comando irreversível e path de credencial são regra de código. Não perguntam ao Jev. Em oneshot sem TTY, `approve` falha fechado, então o gate devolve `block`. API fora do ar em tool gated também devolve `block`.

Cliente: `POST https://api.typesafe.ai/v1/systemone`, model `jev-latest`. A chave fica em `TYPESAFE_API_KEY`. O ledger reescreve strings no formato `apikey_` e `sk-` antes de gravar.

## Instalar

```bash
git clone https://github.com/bbarrosdavi/hermes-jev-decision.git \
  ~/.hermes/plugins/jev-decision
# TYPESAFE_API_KEY no .env do perfil, modo 600. Não commitar.
hermes plugins enable jev-decision
hermes config set context.engine jev-decision
```

Hooks e engine valem no próximo processo desse perfil.

### Claude Code

`claude/` usa o mesmo `forks` e `policy`. O bloco exato vai em `~/.claude/settings.json` e está versionado em [`claude/settings.hooks.json`](claude/settings.hooks.json):

| Hook | Script | Função |
|---|---|---|
| `UserPromptSubmit` | `jev_compact_gate.py` | Com contexto grande, pergunta ao Jev se o pedido abre outra tarefa. Grava o veredito. |
| `PreCompact` (`auto`) | `jev_compact_gate.py` | Com `autoCompactWindow` em 200 mil, o Claude Code propõe compactar cedo. Só passa com veredito de assunto novo ou contexto em 900 mil ou mais. |
| `PreToolUse` (`Agent\|Task`) | `jev_route_agent.py` | O Jev pode trocar o modelo do subagente por um mais barato. Nunca promove, nunca Fable. |

A chave vem de `TYPESAFE_API_KEY` ou de `~/.hermes/.env`. Estado e ledger em `~/.claude/jev/`. `JEV_OFF=1` desliga os dois scripts.

### Relatório mensal

```bash
python3 ~/Projetos/hermes-jev-decision/tools/jev_report.py
```

Sem argumentos: os últimos 30 dias contra a linha de base congelada (`docs/baseline-2026-09.json`, 30/08 a 28/09, antes do Jev). `--since`/`--until` escolhem outro período.

## Documentação

- [Implementação](docs/implementacao.md)
- [Economia de tokens, 2026-09-29](docs/economia-de-tokens-2026-09-29.md): medições, qualidade, projeto complexo, roteamento
- [Primeira comparação, 2026-09-29](docs/resultados-2026-09-29.md)

## Licença

MIT.
