# Primeira comparação, 2026-09-29

Dois perfis, mesma chave de provedor, mesma tarefa, n=1 em cada desenho. A única diferença funcional era o plugin `jev-decision`. O post de origem é [este](https://x.com/N01ennn/status/2103542021071978601).

## Gemini 3.8 Flash

Provider `gemini`, reasoning medium, toolsets `file` e `terminal`. Tarefa: `reconcile` sobre 58 linhas de ledger e 65 de extrato, regras em 4 arquivos AUTHORITATIVE e 8 iscas. Grade por oráculo externo, não pelo unittest do agente.

Preço do medidor Hermes, snapshot `google-pricing-2026-09-02`: input US$ 0,75/M, output US$ 3,75/M, cache read US$ 0,075/M. Status `estimated`.

| | jev | plain | jev menos plain |
|---|---:|---:|---:|
| Grade | passou (48 matches, 8 fee_net) | passou, saída igual | igual |
| Exit | 0 | 0 | |
| Relógio | 150,623 s | 150,692 s | −0,069 s |
| Chamadas Gemini | 17 | 22 | −5 (−22,7%) |
| Input | 139 255 | 175 755 | −36 500 (−20,8%) |
| Output | 26 356 | 27 672 | −1 316 (−4,8%) |
| Cache read | 272 309 | 505 951 | −233 642 (−46,2%) |
| Reasoning | 19 490 | 20 138 | −648 (−3,2%) |
| Total | 437 920 | 709 378 | −271 458 (−38,3%) |
| Custo Gemini | US$ 0,223699 | US$ 0,273533 | −US$ 0,049833 (−18,2%) |

TypeSafe nessa corrida: 13 743 input, 820 output. A US$ 0,042/M só no input, US$ 0,000577. Líquido no `jev`: US$ 0,049256 a menos.

O corte grande foi cache read. Reasoning ficou quase igual. No ledger: gate 26 decisões e 0 bloqueios, compactação 7 chamadas, router `strong` 0,79 não aplicado, classify `technical` 0,71, verificador aceitou com confiança 0,27. Stuck e retrieval não entraram. `data/` idêntico nos dois lados. Qualidade do contrato: empate. O `plain` ficou um pouco mais tipado.

## Grok 4.7, tarefa curta

Mesmo par de perfis, provider `xai-oauth`, tarefa `tokspan` (janela de tokens em stdlib). Grade 9/9 nos dois. O `jev` gastou mais token de fronteira. O extra estava em reasoning (10 342 contra 4 399), não no gate (1,997 s somados). Custo do Grok não saiu da tabela (`cost_status: unknown`). Nessa tarefa o plugin não economizou.

## Leitura

O ganho em dólar apareceu quando o histórico de tool era grande o bastante para a compactação derrubar cache read. Na tarefa curta, o mesmo plugin saiu mais caro. n=1 em cada desenho. 18% não é taxa estável.
