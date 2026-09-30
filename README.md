# jev-decision

Camada de decisão TypeSafe Jev para agentes de código. Funciona no [Hermes Agent](https://github.com/nousresearch/hermes-agent) e no [Claude Code](https://docs.claude.com/en/docs/claude-code). O modelo de fronteira continua gerando o texto. O Jev só classifica: se o pedido abre outra tarefa, se um comando pode rodar, qual modelo basta para um subagente.

Post de origem: [Find every fork your agent pays frontier prices for](https://x.com/N01ennn/status/2103542021071978601).

A camada entra por hooks. O modelo não chama o Jev e o Jev não troca o modelo da conversa: trocar invalidaria o prefix cache e custaria reler todo o contexto.

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `forks.py`, `policy.py`, `client.py`, `ledger.py` | Núcleo comum. Perguntas ao Jev, limiares em código, cliente HTTP, ledger. |
| `__init__.py`, `engine.py`, `delegate.py`, `plugin.yaml` | Plugin do Hermes: hooks, context engine e instâncias filhas. |
| `claude/` | Hooks do Claude Code sobre o mesmo núcleo. |
| `tools/jev_report.py` | Relatório de consumo contra a linha de base. |
| `bench.py`, `test_policy.py` | Benchmark de dois perfis e testes sem rede. |
| `docs/` | Implementação e medições. |

## Como funciona

A economia vem da compactação. O Jev decide **quando** a compactação nativa do agente pode rodar, e o resumo continua sendo o nativo.

Uma vez por pedido do usuário, com o histórico grande, o Jev responde duas perguntas sobre o mesmo estado:

- `new_topic`: o pedido abre uma tarefa sobre outro assunto.
- `refers_back`: o pedido corrige, confirma, responde, continua, cola um erro ou pede status do que veio antes.

Com `new_topic >= 0.70` e `refers_back <= 0.30`, a compactação pode rodar agora. Pedido com menos de 25 caracteres nunca libera. Sem veredito, o agente segue como sem o plugin.

No Claude Code o Jev também escolhe o modelo dos subagentes. Ele só rebaixa (`opus` → `sonnet` → `haiku`), só com confiança >= 0.70 e nunca põe um subagente em Fable. Só é roteado o subagente que herda o modelo da conversa principal. Um `model` na chamada ou na definição do agente é mantido e o Jev nem é consultado; a única exceção é Fable, que vira Opus. `fork`, Explore, Plan, `statusline-setup` e `claude-code-guide` ficam intocados.

Cliente: `POST https://api.typesafe.ai/v1/systemone`, model `jev-latest`. A chave fica em `TYPESAFE_API_KEY`. O ledger reescreve strings no formato `apikey_` e `sk-` antes de gravar. Cada chamada custa ~500 tokens de input e ~500 ms.

## Hermes Agent

| Hook | Função |
|---|---|
| `pre_tool_call` | Gate de `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`. O resto não chama a API. |
| `transform_tool_result` | Stuck a cada chamada de tool. O aviso vai no resultado mais novo. |
| `post_tool_call` | Observa corridas de teste para o verificador. |
| `pre_llm_call` | Router e classify numa chamada, no primeiro turno. Troca de assunto, uma vez por pedido. |
| `pre_verify` | Continua só com evidência: teste falhou, path ausente, ou o Jev confiante de que falta algo. |
| context engine | Herda o `ContextCompressor` nativo. O veredito libera a compactação a partir de 100 mil tokens, e o pedido novo vai como `focus_topic`. O teto nativo (`compression.threshold_tokens`) continua valendo. |

Comando irreversível e path de credencial são regra de código e não perguntam ao Jev. Em oneshot sem TTY, `approve` falha fechado e o gate devolve `block`. API fora do ar em tool gated também devolve `block`. Limiares em `policy.py`: `SAFE_ALLOW` 0.85, `IRREV_ALLOW_MAX` 0.40, `SAFE_BLOCK` 0.20, `IRREV_BLOCK` 0.70, `STUCK_FIRE` 0.75, `DONE_CONTINUE` 0.45.

O Hermes só delega para uma instância filha mais barata quando o pai é cobrado por token e mais caro (`claude`, `gpt-5`, `*-pro`, ou `JEV_ROUTE_FROM`). Pai por assinatura ou local não delega.

### Instalar

```bash
git clone https://github.com/bbarrosdavi/jev-decision.git ~/Projetos/jev-decision
ln -s ~/Projetos/jev-decision ~/.hermes/plugins/jev-decision
# TYPESAFE_API_KEY no .env do perfil, modo 600. Não commitar.
hermes plugins enable jev-decision
hermes config set context.engine jev-decision
```

Hooks e engine valem no próximo processo do perfil. Reinicie o Desktop e o gateway.

## Claude Code

| Hook | Script | Função |
|---|---|---|
| `UserPromptSubmit` | `jev_compact_gate.py` | Com contexto a partir de 150 mil, pergunta ao Jev se o pedido abre outra tarefa. Grava o veredito. |
| `PreCompact` (`auto`) | `jev_compact_gate.py` | Com `autoCompactWindow` em 200 mil, o Claude Code propõe compactar cedo. Só passa com veredito de assunto novo ou contexto em 900 mil ou mais. |
| `PreToolUse` (`Agent\|Task`) | `jev_route_agent.py` | Em subagente que herda o modelo, reescreve o `model` via `updatedInput` quando o Jev escolhe um mais barato. |

### Instalar

1. Clone o repositório (mesmo comando da seção do Hermes).
2. Copie o bloco de [`claude/settings.hooks.json`](claude/settings.hooks.json) para `~/.claude/settings.json`, ajustando o caminho se o clone não estiver em `~/Projetos/jev-decision`.
3. Chave em `TYPESAFE_API_KEY` ou em `~/.hermes/.env`.

Estado e ledger ficam em `~/.claude/jev/`. Para desligar: `JEV_OFF=1` desliga os dois scripts, ou remova os três hooks e o `autoCompactWindow`. Um script com erro nunca bloqueia a sessão: o gate libera a compactação e o roteador deixa o subagente como foi pedido.

## Relatório de consumo

```bash
python3 ~/Projetos/jev-decision/tools/jev_report.py
python3 ~/Projetos/jev-decision/tools/jev_report.py --since 2026-10-01 --until 2026-11-01
```

Lê os transcripts do Claude Code, o `state.db` do Hermes e os dois ledgers. Sem argumentos, compara os últimos 30 dias com a linha de base congelada em [`docs/baseline-2026-09.json`](docs/baseline-2026-09.json) (30/08 a 28/09, antes do Jev). Pesos relativos ao input sem cache: cache read 0,1, cache write 1,25, output 5.

Linha de base: Claude Code com contexto médio de 379 mil e 70% das requisições acima de 200 mil. Hermes com 176 mil de cache read e US$ 0,00974 por chamada.

## Métricas

Medições da configuração atual: gatilho do Hermes em 100 mil, janela do Claude Code em 200 mil, gate em 900 mil, roteador com confiança >= 0.70. Método e detalhes em [`docs/economia-de-tokens-2026-09-29.md`](docs/economia-de-tokens-2026-09-29.md).

### Compactação por troca de assunto

"Custo líquido" já desconta a compactação: leitura do contexto pelo resumidor, saída do resumo e recache do contexto novo.

| Onde | Método | Resultado |
|---|---|---|
| Claude Code, 59 transcripts, 7.853 requisições | simulação com o fork atual | releitura −24,9%, custo líquido −24,1% |
| Hermes, 34 sessões reais, 8.269 chamadas | simulação com o fork atual | custo líquido −13,1% |
| Claude Code ao vivo | três turnos na mesma sessão | bloqueou no pedido dependente, compactou no pedido novo, contexto de 137 mil para 22 mil |
| Hermes ao vivo, depois da revisão do engine | turno após a troca de assunto | compactou de 434 mil para 12 mil caracteres; US$ 0,064 contra 0,165 |

### Qualidade depois de compactar

Teste A → B → A: o turno 1 lê documentos longos, o turno 2 é uma tarefa sem relação (o gatilho compacta), o turno 3 pede um detalhe exato do turno 1, conferido por oráculo. 9 de 9 voltas corretas, inclusive com o detalhe só na conversa e enterrado antes de ~90 mil tokens de leitura.

Projeto de quatro turnos no Hermes (construir um avaliador de planilha, estender, tarefa sem relação, voltar à planilha), suíte oculta de 68 verificações: `jev` 68/68, `plain` 67/68. Na volta ao projeto, o `jev` leu 59 mil de cache por chamada contra 131 mil.

### Roteamento de subagentes (Claude Code)

| Medição | Resultado |
|---|---|
| Pai Opus, tarefas mecânicas delegadas (`lookup`, `extract`, `count`) | 3/3 corretos nos dois braços; custo −39%, −43%, −34% |
| Revisão de código delegada | Opus mantido |

### Limitações

- Precisão do veredito: no Claude Code, uns 3 de 16 disparos eram continuação. O custo do erro é uma compactação antecipada e alguma releitura.
- Os testes ao vivo são n=1 ou n=2. As porcentagens não são taxa estável.
- A economia depende de sessões longas depois de uma troca de assunto. Em trabalho contínuo sobre um tema só, a camada não age e a compactação acontece no teto nativo.

## Testes

```bash
PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python test_policy.py
```

Sem rede. Os testes dos hooks do Hermes precisam do pacote do Hermes no path.

## Documentação

- [Implementação](docs/implementacao.md)
- [Economia de tokens, 2026-09-29](docs/economia-de-tokens-2026-09-29.md)

## Licença

MIT.
