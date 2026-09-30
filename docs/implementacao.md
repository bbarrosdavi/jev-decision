# Implementação

Origem: [N01ennn, 25 set 2026](https://x.com/N01ennn/status/2103542021071978601). A tese do post é tirar do modelo de fronteira os forks que não produzem texto guardado. A decisão fica fora da thread principal para não invalidar o prefix cache.

Revisão de 2026-09-29: ver [economia de tokens](economia-de-tokens-2026-09-29.md) para as medições que motivaram cada mudança.

## Arquivos

| Arquivo | Papel |
|---|---|
| `plugin.yaml` | Nome `jev-decision`. Exige `TYPESAFE_API_KEY`. |
| `__init__.py` | Hooks do Hermes: `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `pre_llm_call`, `pre_verify`, `on_session_end`. |
| `client.py` | HTTP stdlib para `api.typesafe.ai`. Uma nova tentativa em erro de transporte, 429 e 5xx. |
| `policy.py` | Regras e limiares. Sem rede. |
| `forks.py` | Perguntas ao Jev: gate, stuck, completion, topic_shift, route, classify, dispatch. |
| `engine.py` | Engine de contexto: o compressor nativo com um gatilho a mais. |
| `ledger.py` | Append em `logs/jev-decisions.jsonl`, com `ts` e redação de chave. |
| `delegate.py` | `jev_delegate`: instância filha no modelo que o Jev escolhe. |
| `claude/jev_compact_gate.py` | Claude Code: gate de compactação (`UserPromptSubmit`, `PreCompact`). |
| `claude/jev_route_agent.py` | Claude Code: modelo do subagente (`PreToolUse` na tool `Agent`). |
| `claude/settings.hooks.json` | O bloco de `~/.claude/settings.json` em uso. |
| `tools/jev_report.py` | Relatório do período contra a linha de base `docs/baseline-2026-09.json`. |
| `test_policy.py` | Testes sem rede. |
| `bench.py` | Latência e tokens de chamadas isoladas. |

## Compactação: o nativo faz, o Jev decide quando

A compactação nativa continua dona do contexto: limiar, histerese, ociosidade, resumo. O Jev não reescreve o request.

O custo real das sessões longas é o contexto relido a cada chamada. No Hermes padrão, `gemini-3.8-flash` relê em média 202 mil tokens por chamada, e a compressão nativa só dispara em 50% de uma janela de 1M. No Claude Code, a compactação padrão de modelos 1M é em ~967 mil.

O gatilho: uma vez por pedido do usuário, com o histórico grande, o Jev responde duas perguntas sobre o mesmo estado (`forks.topic_shift`):

- `new_topic`: o pedido abre uma tarefa sobre outro assunto
- `refers_back`: o pedido corrige, confirma, responde, continua, cola um erro ou saída, ou pede status do trabalho anterior

`policy.topic_compact` libera a compactação com `new_topic >= 0.70` e `refers_back <= 0.30`. Pedido com menos de 25 caracteres nunca libera e não chama o Jev.

Hermes: `pre_llm_call` grava o veredito em `HERMES_HOME/cache/jev-topic/<sessão>.json`. O veredito fica em disco porque o Hermes carrega o engine e os hooks como duas cópias do módulo, e um dicionário em memória não é compartilhado entre elas.

O engine é construído com as mesmas chaves de `compression` que o `agent_init` passa ao compressor nativo, sobre o `DEFAULT_CONFIG` (inclusive o teto `threshold_tokens` de 256 mil) e com a saída reservada do Gemini. Sem veredito, o comportamento é o do `plain`.

`should_compress_info` só lê: com o nativo sem pressa e sem bloqueio (`_compression_block_reason()` vazio), contexto em 100 mil ou mais (`JEV_EARLY_COMPACT_TOKENS`) e veredito pendente, devolve `True`. O `compress()` passa o pedido novo como `focus_topic`, consome o veredito e registra `compacted` ou `no_progress` com caracteres antes e depois.

Claude Code: `autoCompactWindow` em 200 mil faz o Claude Code propor a compactação cedo. `UserPromptSubmit` grava o veredito em `~/.claude/jev/state/<sessão>.json`. `PreCompact` com matcher `auto` bloqueia, exceto com veredito de assunto novo ou contexto em 900 mil ou mais (`JEV_HARD_LIMIT`). Compactação manual nunca é bloqueada. Sem veredito, ou com o Jev fora do ar, a sessão se comporta como antes.

## Modelo do subagente (Claude Code)

`jev_route_agent.py`, `PreToolUse` com matcher `Agent|Task`. O modelo pedido é o explícito na chamada, senão o da definição do agente (`~/.claude/agents/*.md`, `.claude/agents/`), senão o do pai para `general-purpose` (lido do transcript). `fork`, `Explore`, `Plan` e agentes com modelo próprio ficam intocados; pedido já em `haiku` não chama o Jev.

`forks.route_subagent` pergunta ao Jev entre `haiku` (achar, extrair, rodar e reportar, despachar), `sonnet` (mudança localizada, revisão curta, resumo) e `opus` (arquitetura, pesquisa aberta, revisão adversarial, julgamento caro). `policy.route_subagent` aplica só se for mais barato que o pedido e com confiança >= 0.70. Fable vira no máximo Opus, mesmo com o Jev fora. A saída é `updatedInput` com o resto da chamada intacto; a decisão de permissão não muda.

`JEV_OFF=1` desliga os dois scripts do Claude Code.

## Gate

Tools gated: `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`, `jev_delegate`.

`classify` devolve `skip`, `irreversible` ou `gate`. Edição de arquivo é julgada pelo path (credencial ou fora do cwd), não pelo texto que escreve. Comando é julgado pelo texto.

Regra de código `irreversible`: `git checkout/restore/reset/clean/rm`, `git branch -D`, `git stash drop/clear`, qualquer `rm` com argumento, `find ... -delete`, `shred`, `unlink`, `sudo`, `mkfs`/`shutdown`, `curl|sh`, `dd if=`, `chmod -R 777`, `push --force`, `drop table`, `truncate table`, fork bomb, path de credencial.

`compose_gate`:

- regra de código: `approve`, sem chamada de API
- `costs_money` ou `externally_visible` >= 0.70: `approve`
- `safe >= 0.85` e `irreversible < 0.40`: allow
- `safe <= 0.20` e `irreversible >= 0.70`: block
- faixa do meio em `terminal`/`execute_code`: `approve`
- faixa do meio em edição de arquivo: allow
- API fora (depois da nova tentativa): block

Toda mensagem de `approve`/`block` manda não repetir a chamada nem uma equivalente, terminar o resto e reportar o passo como pendente. O veredito de uma chamada idêntica sobre o mesmo texto de arquivo fica em cache no processo.

## Stuck

`transform_tool_result`, a cada chamada de tool. O aviso vai anexado ao resultado mais novo, então o prefixo cacheado não muda.

- regra de código: a mesma chamada com o mesmo resultado 3 vezes nas últimas 6
- Jev: a partir da 5ª ação, a cada 4, as duas perguntas do post (`progressing`, `repeating`) sobre as últimas 5 ações e observações
- dispara com `repeating >= 0.75`, ou `progressing < 0.30` e `repeating >= 0.50`
- no máximo 2 avisos por sessão

## Completion

`pre_verify`, uma vez por turno. Ordem:

1. último teste observado falhou (`post_tool_call` com exit code ou `FAILED`): `continue` com o comando e o fim da saída, sem Jev
2. último teste passou, ou o texto final mostra a corrida OK, ou o unittest do workspace passa: aceita, sem Jev
3. senão o Jev recebe pedido, resposta, paths e evidência (último teste e início de cada arquivo alterado)

`policy.verify_continue` só abre outro turno com path ausente ou com o Jev confiante (`confidence >= 0.5`) de que a resposta não atende (`quality < 1.0`). Dúvida encerra e fica no ledger.

## Router, classify, dispatch

`route_model` e `classify` saem numa chamada só, uma vez por sessão, e vão para o ledger. O router não troca o modelo do chat. `fast` manda o turno chamar `jev_delegate` só quando o pai é cobrado por token e mais caro que `FAST_ROUTE` (`ROUTE_FROM`: `claude`, `gpt-5`, `*-pro`), e `jev_delegate` nunca delega de provedor por assinatura ou local (`*-oauth`, `ollama`, `lmstudio`, `llamacpp`).

`delegate_task` que passou no gate chama `dispatch_worker`. Confiança abaixo da barra (0.85, sobe 0.05 se o grounded médio recente < 0.40) força `review`.
