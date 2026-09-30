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

No Claude Code o Jev também escolhe o modelo dos subagentes. Ele só rebaixa (`opus` → `sonnet` → `haiku`), só com confiança >= 0.70 e nunca põe um subagente em Fable. `fork`, Explore, Plan, `statusline-setup` e `claude-code-guide` ficam intocados. Um `model` explícito na chamada ou na definição do agente vale como teto, não como trava: o Jev ainda pode rebaixar abaixo dele.

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
| `PreToolUse` (`Agent\|Task`) | `jev_route_agent.py` | Reescreve o `model` do subagente via `updatedInput` quando o Jev escolhe um mais barato. |

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

Medições de 29/09/2026. Detalhes, custos por turno e método em [`docs/economia-de-tokens-2026-09-29.md`](docs/economia-de-tokens-2026-09-29.md).

### Compactação por troca de assunto

"Custo líquido" já desconta a compactação: leitura do contexto pelo resumidor, saída do resumo e recache do contexto novo.

| Onde | Método | Resultado |
|---|---|---|
| Claude Code, 59 transcripts, 7.853 requisições | simulação com o fork real | releitura −24,9%, custo líquido −24,1% |
| Hermes, 34 sessões reais, 8.269 chamadas | simulação com o fork real | releitura −13,6%, custo líquido −12,4% |
| Hermes ao vivo, dois turnos, n=2 | `jev` × `plain`, mesmo modelo | turno após a troca: custo −58%, releitura −81%, notas iguais |
| Claude Code ao vivo, Haiku headless | três turnos na mesma sessão | bloqueou no pedido dependente, compactou no pedido novo, contexto de 137 mil para 22 mil |

Janela do gatilho, custo líquido simulado:

| Gatilho | Hermes | Claude Code |
|---|---:|---:|
| 200 mil | | 24,1% |
| 150 mil | 12,4% | 24,6% |
| 120 mil | 12,9% | 24,4% |
| 100 mil | 13,1% | |
| 80 mil | 14,8% | |

O Hermes usa 100 mil e o Claude Code 200 mil.

### Qualidade depois de compactar

Teste A → B → A: o turno 1 lê documentos longos, o turno 2 é uma tarefa sem relação (o gatilho compacta), o turno 3 pede um detalhe exato do turno 1, conferido por oráculo.

| Cenário | Resultado |
|---|---|
| Hermes, detalhe que está em arquivo, n=2 | corretos; o `jev` releu os arquivos e custou US$ 0,046 e 0,015 contra 0,123 e 0,134 |
| Hermes, detalhe só na conversa, avisado, no início ou no meio, n=4 | corretos |
| Hermes, detalhe só na conversa, enterrado antes de ~90 mil tokens de leitura, n=2 | corretos; o valor ficou no resumo |
| Claude Code, Opus, token impresso antes de ~137 mil tokens de texto | correto |

9 de 9 voltas ao assunto antigo corretas.

### Roteamento de subagentes (Claude Code)

| Medição | Resultado |
|---|---|
| Uso de subagentes em 59 sessões | 95 chamadas, 92 em Opus, 7,5% do custo ponderado |
| Réplica das 95 chamadas | 10 rebaixadas para Haiku (lotes de geração de consultas, confiança 0,90 a 0,93); revisões e resumos mantidos |
| Ao vivo, pai Opus, `lookup`, `extract`, `count` | 3/3 corretos nos dois braços; custo −39%, −43%, −34% |
| Ao vivo, revisão de código | Opus mantido nos dois braços |

### Projeto complexo, quatro turnos

Construir um avaliador de planilha, estender, uma tarefa sem relação, voltar à planilha. Nota por suíte oculta de 68 verificações.

| Braço | Custo | Suíte oculta |
|---|---:|---:|
| Claude Code + Jev, Opus | US$ 8,10 | 68/68 |
| Claude Code puro, Opus | US$ 7,33 | 68/68 |
| Hermes `jev`, gatilho em 150 mil | US$ 1,131 | 68/68 |
| Hermes `plain` | US$ 1,186 | 68/68 |
| Hermes `jev`, gatilho em 100 mil | turnos 3+4: US$ 0,381 | 68/68 |
| Hermes `plain` | turnos 3+4: US$ 0,388 | 67/68 |

Num projeto de um assunto só, que cabe na janela, a camada fica fora do caminho. A diferença de US$ 0,77 no Claude Code é variação do Opus: nenhum hook agiu. Depois da compactação, o turno de volta leu 59 mil de cache por chamada contra 131 mil.

### Suíte de cinco tarefas, Hermes, Gemini 3.8 Flash

| Tarefa | Nota | `jev` | `plain` |
|---|---|---:|---:|
| recon | os dois passaram | US$ 0,314, 31 chamadas | US$ 0,239, 17 chamadas |
| span | os dois passaram | US$ 0,192, 56 s | US$ 0,145, 417 s |
| gate | `add` correto nos dois | canário sobreviveu, US$ 0,250 | canário apagado, US$ 0,018 |
| spec | os dois passaram | US$ 0,096 | US$ 0,048 |
| repair | os dois passaram | US$ 0,035 | US$ 0,035 |

Essa suíte rodou com o desenho original (verificador que forçava turnos e compactação por request), que gastava mais do que economizava. Total: `jev` US$ 0,887 contra `plain` US$ 0,485. Esses caminhos foram corrigidos ou descartados. O canário do `gate` sobreviveu porque o agente leu a nota da tarefa, não por bloqueio do gate. Em tarefa curta, sem troca de assunto, a camada não economiza.

Primeira comparação, tarefa `reconcile` com 58 linhas de ledger: total de tokens −38,3%, custo Gemini −18,2%, mesma nota. n=1.

### Medido e descartado

| Alavanca | Medição |
|---|---|
| Filtrar saída de tool no Claude Code | cortaria 1,1%, e os blocos cortados tinham identificadores usados depois |
| Compactação por request no Hermes | total de tokens −24%, custo +3%: cada corte quebra o prefix cache |
| Pré-busca de leitura previsível | 1,5% das requisições no Claude Code, 5% no Hermes |
| Detector de ciclo | 1,1% das chamadas repetem numa janela de 6 |

### Limitações

- Precisão do veredito: no Claude Code, uns 3 de 16 disparos eram continuação. O custo do erro é uma compactação antecipada e alguma releitura. O resumo nativo mantém o fio.
- Os testes ao vivo são n=1 ou n=2. As porcentagens não são taxa estável.
- A economia depende de sessões longas depois de uma troca de assunto. Em trabalho contínuo sobre um tema só, a camada não tem o que fazer.

## Testes

```bash
PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python test_policy.py
```

Sem rede. Os testes dos hooks do Hermes precisam do pacote do Hermes no path.

## Documentação

- [Implementação](docs/implementacao.md)
- [Economia de tokens, 2026-09-29](docs/economia-de-tokens-2026-09-29.md)
- [Suíte de cinco tarefas](docs/resultados-suite.md)
- [Primeira comparação](docs/resultados-2026-09-29.md)

## Licença

MIT.
