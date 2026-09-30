# Economia de tokens com o Jev, 2026-09-29

O plugin original gastava mais token do que economizava. A economia medida veio de outro lugar: o Jev decide **quando** a compactação nativa roda, e a compactação continua sendo a do Hermes e a do Claude Code.

## Resultado

| Onde | Método | Resultado |
|---|---|---|
| Claude Code, 59 transcripts, 7.853 requisições | simulação com o fork real | releitura −24,9%, custo líquido −24,1% |
| Hermes, 34 sessões reais do perfil padrão, 8.269 chamadas | simulação com o fork real | releitura −13,6%, custo líquido −12,4% |
| Hermes ao vivo, dois turnos, n=2 | `jev` × `plain`, mesmo modelo | turno após a troca de assunto: custo −58%, releitura −81%, notas iguais |
| Claude Code ao vivo, Haiku headless | três turnos na mesma sessão | bloqueou no pedido dependente, compactou no pedido novo, contexto de 137 mil para 22 mil |
| Claude Code ao vivo, subagentes | pai Opus, tarefas mecânicas delegadas | custo −34% a −43%, respostas iguais, julgamento mantido em Opus |

"Custo líquido" já desconta as compactações: leitura do contexto pelo resumidor, saída do resumo e recache do contexto novo. Pesos relativos ao input: cache read 0,1, cache write 1,25 (Claude), output 5.

## Mecanismo

Uma vez por pedido do usuário, com o histórico grande, o Jev responde duas perguntas sobre o mesmo estado: `new_topic` (o pedido abre outra tarefa) e `refers_back` (o pedido corrige, confirma, responde, continua, cola um erro ou pede status do que veio antes). Com `new_topic >= 0.70` e `refers_back <= 0.30`, a compactação nativa pode rodar agora. Pedido com menos de 25 caracteres nunca libera.

- Hermes: o engine herda o `ContextCompressor`. O veredito libera o `should_compress` a partir de 100 mil tokens, e o pedido novo vai como `focus_topic`.
- Claude Code: `autoCompactWindow` em 200 mil faz o Claude Code propor cedo. O hook `PreCompact` bloqueia, exceto com veredito de assunto novo ou contexto em 900 mil ou mais.

A chamada ao Jev custa ~500 tokens de input (US$ 0,00002) e ~500 ms, uma vez por pedido.

## Qualidade depois da compactação

O risco da compactação cedo é o usuário voltar ao assunto antigo. Teste A → B → A: turno 1 lê 10 documentos longos, turno 2 é uma tarefa sem relação (o gatilho compacta), turno 3 pede um detalhe exato do turno 1, conferido por oráculo.

| Cenário | Resultado |
|---|---|
| Hermes, detalhe que está em arquivo (`stage`, `units` de dois documentos), n=2 | `jev` e `plain` corretos; o `jev` releu os arquivos e custou US$ 0,046 e 0,015 contra 0,123 e 0,134 |
| Hermes, detalhe só na conversa (token aleatório impresso uma vez), avisado, no início ou no meio, n=4 | corretos |
| Hermes, detalhe só na conversa, enterrado depois de 1 documento e antes de ~90 mil tokens de leitura, n=2 | corretos; a mensagem com o token foi compactada (`compacted=1`) e o valor ficou no resumo |
| Claude Code, Opus, token impresso antes de ~137 mil tokens de texto, compactação liberada pelo gate | correto (`86c3164f`) |

## Roteamento de subagentes no Claude Code

`PreToolUse` na tool `Agent`: o Jev escolhe entre `haiku`, `sonnet` e `opus` e o hook reescreve o campo `model` via `updatedInput`. O modelo da conversa principal nunca troca: trocar custaria reler todo o contexto sem cache. O subagente nasce com contexto próprio.

Regras em código: só rebaixa, só com confiança >= 0.70; o modelo pedido é o explícito, o da definição do agente ou o do pai (lido do transcript); `fork` e agentes com modelo próprio (Explore, `feynman-*`) ficam intocados; subagente nunca roda em Fable.

| Medição | Resultado |
|---|---|
| Uso de subagentes em 59 sessões | 95 chamadas, 92 em Opus, 7,5% do custo ponderado |
| Réplica das 95 chamadas | 10 rebaixadas para Haiku (lotes de geração de consultas, confiança 0,90 a 0,93); revisões e resumos mantidos |
| Ao vivo, pai Opus, subagente pedido em Opus: `lookup`, `extract`, `count` | 3/3 corretos nos dois braços; custo −39%, −43%, −34% |
| Ao vivo, tarefa de julgamento (revisão de código) | Opus mantido nos dois braços |
| Nesta sessão | subagente pedido em Opus rodou em Haiku e respondeu certo |

No Hermes o roteamento não economiza no uso atual: o modelo padrão já é o `gemini-3.8-flash`, destino do roteamento, e o Grok roda por `xai-oauth` (assinatura). A regra antiga delegaria trabalho do Grok para o Gemini pago. Agora o Hermes só delega de pai cobrado por token e mais caro (`claude`, `gpt-5`, `*-pro`, `JEV_ROUTE_FROM`), nunca de provedor por assinatura ou local.

## Projeto complexo, quatro turnos

Uma sessão de desenvolvimento realista na mesma sessão: (1) construir um avaliador de planilha (tokenizer, parser com precedência, grafo com detecção de ciclo, propagação de erros, funções sobre intervalos, CLI), (2) adicionar `ROUND` e `ABS`, (3) uma tarefa sem relação (`wordfreq`), (4) voltar à planilha e aceitar referências absolutas. Nota por suíte oculta de 68 verificações, validada antes contra uma implementação de referência (68/68). No Claude Code o prompt pede arquitetura primeiro e subagentes nas partes independentes.

| Braço | Custo | Suíte oculta | Ação da camada |
|---|---:|---:|---|
| Claude Code + Jev, Opus | US$ 8,10 | 68/68 | 3 subagentes de construção mantidos em Opus (Jev com confiança 0,10 a 0,28); contexto abaixo da janela de 200 mil, gate sem ação |
| Claude Code puro, Opus | US$ 7,33 | 68/68 | 3 subagentes em Opus |
| Hermes `jev` (gatilho em 150 mil) | US$ 1,131 | 68/68 | nenhum bloqueio, aviso de ciclo ou turno forçado; veredito de troca de assunto certo no turno 3, contexto de ~108 mil abaixo do gatilho |
| Hermes `plain` | US$ 1,186 | 68/68 | |

Qualidade igual nos quatro. Num projeto de um assunto só, que cabe na janela, a camada fica fora do caminho. A diferença no Claude Code é variação do Opus: no turno 1 o braço Jev fez 15 passos contra 8, e nenhum hook injetou texto no contexto.

Repetição dos dois braços do Hermes com o gatilho em 100 mil:

| Turno | `jev` | `plain` |
|---|---:|---:|
| 1 construir | US$ 0,618 | US$ 0,582 |
| 2 estender (Jev: depende do anterior, não compactou) | US$ 0,441 | US$ 0,341 |
| 3 tarefa sem relação (compactou a 132 mil) | US$ 0,127 | US$ 0,092 |
| 4 volta à planilha | US$ 0,254, 59 mil de cache read por chamada | US$ 0,296, 131 mil por chamada |
| Suíte oculta | 68/68 | 67/68 |

A volta ao projeto depois de compactar manteve a qualidade. Os turnos 3 e 4 juntos empataram (US$ 0,381 contra 0,388): a compactação cobra na hora (resumo auxiliar e recache do prefixo novo) e devolve a cada chamada seguinte, e houve só 28 chamadas depois da troca. Nos turnos 1 e 2 o plugin não agiu (sem bloqueio, aviso ou turno forçado no ledger); a diferença ali é variação do agente. Num projeto curto a camada fica dentro do ruído; a economia depende de sessões longas depois da troca de assunto.

O teste mostrou que o gatilho do Hermes em 150 mil estava alto para sessões de trabalho. Varredura nas 34 sessões reais (custo líquido): 150 mil 12,4%, 120 mil 12,9%, 100 mil 13,1%, 80 mil 14,8%. O padrão passou a 100 mil. No Claude Code a janela quase não importa: 200 mil 24,1%, 150 mil 24,6%, 120 mil 24,4%; ficou em 200 mil.

## Revisão do engine em Opus

A tarefa de julgamento do teste de roteamento pediu a um subagente Opus que revisasse o `engine.py`. As duas revisões independentes acharam os mesmos defeitos, todos corrigidos:

- O engine não repassava o teto nativo `compression.threshold_tokens` (256 mil no `DEFAULT_CONFIG`). Numa janela de 1M compactava em 500 mil, mais tarde que o `plain`. Agora o compressor é construído com as mesmas chaves que o `agent_init` usa, e a saída reservada do Gemini (65.535) também.
- O veredito era consumido na decisão (`should_compress`), antes de qualquer compactação. Se o host pulasse o `compress()`, o gatilho se perdia e o `focus_topic` vazava para uma compactação posterior. Agora a decisão só lê; o consumo acontece no `compress()`.
- Abaixo do limiar, o nativo devolve `False` antes de checar cooldown e disjuntor, e o gatilho ignorava esses bloqueios. Agora consulta `_compression_block_reason()` antes.
- O ledger registrava "compacted" mesmo sem encolher. Agora registra `compacted` ou `no_progress`, com caracteres antes e depois.

Depois da correção, a verificação ao vivo compactou de 434 mil para 12 mil caracteres, e o turno seguinte custou US$ 0,064 contra 0,165.

## O que custava tokens no desenho original

**Verificador.** Na dúvida, forçava outro turno. Em 12 veredictos, 10 foram `continue`, com confiança mediana 0,42. O estado não tinha evidência (nem saída de teste, nem conteúdo de arquivo). Na tarefa `gate`, a mensagem "Run the tests" numa tarefa sem testes gerou 24 chamadas a mais: busca de testes, grep em `/home/davi` (timeout de 180 s) e leitura do `run_suite.py`, o oráculo do benchmark.

**Compactação por request.** 38 de 47 resultados viraram stub, com relevância mediana 0,08. A pergunta para `read_file` era "o documento responde ao goal", e um CSV de dados nunca responde. No `recon`, `ledger.csv` e `bank.csv` foram relidos 4 vezes cada; o `plain` leu cada arquivo uma vez. Corrigida a pergunta (score de 3 níveis com confiança), o corte ficou só nas iscas, e ainda assim o custo subiu: total de tokens −24%, custo +3%. Cada corte quebra o prefix cache: cache read caiu de 452 mil para 254 mil, e input fresco subiu de 116 mil para 177 mil, a 10 vezes o preço.

**Engine sem compressão.** O limiar era 10¹². No perfil padrão, a sessão das 17:00 de 29/09 fez 673 chamadas sem nenhuma compactação, com média de 367 mil tokens por chamada e US$ 25,02. As sessões grandes de 26 e 27/09, antes do plugin, compactaram e ficaram em ~150 mil por chamada.

**Stuck.** Rodava no `pre_llm_call`, que o Hermes chama uma vez por pedido do usuário. Numa sessão oneshot nunca disparava.

**Correção do relatório da suíte.** Na tarefa `gate`, o canário do `jev` sobreviveu porque o agente leu `note.md` e não tentou o `rm`. O gate não bloqueou nada.

## Medido e descartado no Claude Code

| Alavanca | Medição |
|---|---|
| Filtrar saída de tool (`updatedToolOutput`) | 60 saídas grandes de Bash: o Jev cortaria 1,1%, e os 3 blocos cortados tinham identificadores usados depois |
| `allow` do Jev para pular o classificador do modo auto | sem economia documentada em conta Max; troca um juiz que vê o transcript por um que só vê o comando |
| Pré-busca de leitura previsível | 1,5% das requisições no Claude Code, 5% no Hermes |
| Detector de ciclo | 1,1% das chamadas repetem numa janela de 6 |
| Compactação de mensagens antigas por hook | impossível: `PreCompact` só bloqueia |

## Outras correções

- `client.py`: uma nova tentativa em erro de transporte, 429 e 5xx. Uma queda de Wi-Fi fazia o gate falhar fechado num `write_file`.
- Gate: `rm` sem flag, `find -delete`, `git branch -D` viraram regra de código; edição de arquivo julgada pelo path, não pelo texto; mensagem que manda não repetir a chamada; cache de veredito.
- Verificador: teste observado decide sem Jev; o Jev só abre turno quando está confiante de que falta algo.
- Stuck em `transform_tool_result`, a cada chamada, com as duas perguntas do post e uma regra de código para chamada idêntica.
- Router e classify numa chamada só.

## Limitações

- Precisão do veredito: no Claude Code, uns 3 de 16 disparos eram continuação ("mexer como, treinar um classificador de prompt antes da geração?"). O custo do erro é uma compactação antecipada e alguma releitura; o resumo nativo mantém o fio.
- A simulação do Hermes reconstrói o contexto pelo tamanho das mensagens, calibrado no total cobrado de cada sessão.
- Live com n=2 no Hermes e um teste de mecanismo no Claude Code.

## Onde está ativo

- Hermes: perfis `default` e `jev` (`context.engine: jev-decision`). Vale no próximo processo: reiniciar o Desktop e o gateway.
- Claude Code: `~/.claude/settings.json` (`autoCompactWindow` e os hooks `UserPromptSubmit`, `PreCompact` e `PreToolUse`). Para desligar, remover os quatro ou exportar `JEV_OFF=1`.

Verificação mensal: `docs/baseline-2026-09.json` congela 30/08 a 28/09, antes do Jev (Claude Code apaga transcripts com mais de 30 dias). Um mês depois:

```bash
python3 ~/Projetos/jev-decision/tools/jev_report.py --since 2026-10-01 --until 2026-11-01
```

Roda de qualquer diretório. Sem argumentos, compara os últimos 30 dias com a linha de base.

Linha de base: Claude Code com contexto médio de 379 mil e 70% das requisições acima de 200 mil; Hermes com 176 mil de cache read e US$ 0,00974 por chamada. O relatório também estima o efeito direto: tokens não relidos depois de cada compactação liberada pelo Jev e custo evitado nos subagentes rebaixados.

Reproduzir: `test_policy.py` (sem rede); benchmark de dois turnos e simulações descritos acima, scripts no scratchpad da sessão de 29/09.
