# hermes-jev-decision

Camada de decisão TypeSafe Jev ao lado do loop do [Hermes Agent](https://github.com/nousresearch/hermes-agent). O modelo de fronteira continua gerando o texto. O Jev só classifica.

Post de origem: [Find every fork your agent pays frontier prices for](https://x.com/N01ennn/status/2103542021071978601).

Este plugin não é o `jev-typesafe` do catálogo. Aquele expõe tools (`jev_check`, `jev_route`, `jev_score`, `jev_evaluate`) que o modelo precisa chamar. Este registra hooks. O modelo não chama o Jev.

## O que entra no loop

| Hook | Função |
|---|---|
| `pre_tool_call` | Gate de `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`. O resto não chama a API. |
| `pre_llm_call` | Router e classify no primeiro turno, só no ledger. Stuck, se disparar, anexa um contexto na mensagem do usuário. Não mexe no system prompt. |
| `pre_verify` | Um nudge de continuação. `attempt > 0` não chama de novo. |
| context engine | Compactação só no request. O que fica, fica verbatim. Resultado de tool descartado vira stub. |

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

## Documentação

- [Implementação](docs/implementacao.md)
- [Primeira comparação, 2026-09-29](docs/resultados-2026-09-29.md)

## Licença

MIT.
