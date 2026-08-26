# Relatório PDF profissional — Design

## Contexto

O usuário quer, a partir do fluxo "obra inteira" já existente no Auditor RDO
(baixar fotos de todas as tarefas de uma obra), gerar um PDF com o mesmo
conteúdo do RDO oficial do sistema Diário de Obra — cabeçalho da obra,
tabela de atividades e galeria de fotos — só que com design mais bonito e
profissional. Sem os vereditos de IA (o usuário pediu explicitamente pra
não incluir, pra ficar fiel ao conteúdo do relatório original). Sem
gerar dependência pesada no deploy (Render free tier).

Exemplo de referência: PDF anexado pelo usuário (RDO nº 1, obra de um
cliente real usado como modelo).

## Decisões já tomadas (aprovadas pelo usuário)

- Escopo: só o fluxo de **obra inteira** (URL de `lista-de-tarefas`), não o
  modo "relatório específico" (um dia).
- Sem vereditos de IA no PDF — só o conteúdo original, visual melhor.
- Formato de entrega: PDF pra baixar (botão novo, ao lado de "Baixar .zip").
- Geração: **ReportLab** (Python puro, sem dependência de sistema — funciona
  igual no Windows local e no Render free tier).
- Cabeçalho leva a logo da officez (extraída do PDF de exemplo, salva em
  `static/officez_logo.png`) + dados do cliente/obra vindos da API.

## Dados disponíveis na API (investigado ao vivo)

`GET /obras/{obra_id}` já retorna tudo que aparece na tela "Cadastro da
obra" do sistema:

```
nome, numeroContrato, cliente, endereco, responsavel,
dataInicio, dataFim, prazo: {contratual, decorrido, aVencer},
status: {descricao}, fotoUrl (imagem/logo cadastrada na obra — nem
sempre é uma logo, pode ser foto do canteiro; usar se existir, omitir
se não houver)
```

`GET /obras/{obra_id}/lista-de-tarefas` (já é chamado por
`montar_mapa_tarefas`) retorna, por grupo (`cronograma[]`), a lista de
tarefas com:

```
grupo.item (letra), grupo.descricao
tarefa.item (número), tarefa.descricao (com o bloco de cronograma
  embutido — precisa limpar pra exibição), tarefa.porcentagem (0-100),
  tarefa.controleDeProducao: {quantidade, unidade} (se ativo)
```

Isso cobre 100% do que o relatório original mostra — nenhum endpoint novo
é necessário.

## Arquitetura

1. **`auditar_relatorio.py`** — estender `montar_mapa_tarefas` para, na
   mesma passada que já faz hoje (sem chamada extra à API), também montar
   a lista completa de atividades (todas as 15 tarefas, não só as que têm
   foto) com porcentagem/quantidade. Passa a retornar
   `(mapa, atividades)` em vez de só `mapa` — os 2 call-sites existentes
   (`coletar_fotos` em modo relatório, `coletar_fotos_obra` em modo obra)
   são atualizados para desempacotar a tupla.
2. **`coletar_fotos_obra`** — hoje já chama `GET /obras/{obra_id}` pra
   pegar só o `nome`; passa a guardar o dicionário inteiro (cabeçalho) no
   `contexto` retornado, junto com a lista de atividades do passo 1.
3. **`relatorio_pdf.py`** (novo módulo) — função
   `gerar_pdf(cabecalho: dict, atividades: list, fotos: list[Foto],
   logo_path: Path, saida: Path | BytesIO) -> None` que monta o PDF com
   ReportLab (`platypus`): `BaseDocTemplate` com `PageTemplate` custom
   pro cabeçalho/rodapé de cada página, `Table` pra atividades, grid de
   `Image` + `Paragraph` pras fotos.
4. **`app.py`** — rota nova `GET /api/pdf/<zid>`, irmã de `/api/zip/<zid>`:
   abre o zip já pronto (guardado em `_zips[zid]`), lê os bytes das fotos
   direto de dentro do zip (sem rebaixar), chama `relatorio_pdf.gerar_pdf`
   e devolve como download (`Content-Disposition: attachment`). Gera na
   hora, não fica cacheado em disco (ReportLab é rápido o suficiente).
   Para isso, `_zips[zid]` passa a guardar também `info` (cabeçalho +
   atividades), setado no momento em que o zip é criado (dentro de
   `/api/processar`).
5. **`templates/index.html`** — botão novo "Baixar relatório PDF" ao lado
   do "Baixar .zip" existente (`#btnZip`), reaproveitando `ESTADO.zipId`
   já guardado.
6. **`static/officez_logo.png`** — já criado (extraído do PDF de
   exemplo, fundo transparente).

## Layout do PDF

- **Cabeçalho** (primeira página): logo officez no topo, título
  "Relatório de Obra" + nome da obra em destaque. Bloco de informação em
  grade (não tabela apertada): Contrato, Cliente, Status, Endereço,
  Responsável, Prazo contratual/decorrido/a vencer. Se `fotoUrl` existir,
  mostra a imagem da obra ao lado. Faixa de resumo: Tarefas, Fotos,
  Grupos.
- **Atividades**: agrupadas por grupo (faixa colorida com letra +
  descrição do grupo), cada tarefa numa linha: código, descrição limpa
  (sem o bloco de cronograma duplicado), quantidade/unidade, selo de
  status colorido (verde "Concluída" em 100%, azul "Em andamento" entre
  1-99%, cinza "Não iniciada" em 0%).
- **Fotos**: agrupadas por grupo/tarefa igual à estrutura de pastas do
  zip. Grid de 2 colunas, legenda limpa "código — descrição" (sem repetir
  o cronograma).
- **Rodapé**: número de página + "Gerado por Auditor RDO em DD/MM/AAAA
  HH:MM".
- Fonte: Helvetica padrão do ReportLab (sem custom font embutida).
- Paleta: azul de marca do Auditor RDO (`#1f6feb`) nos títulos de seção e
  selos.

## Tratamento de erros

- Zip expirado/inexistente (`zid` não encontrado em `_zips`): 404 com
  mensagem clara, igual ao comportamento atual de `/api/zip/<zid>`.
- Foto ausente dentro do zip (falha no download original): a célula da
  foto no PDF mostra um placeholder "foto indisponível" em vez de
  quebrar a geração inteira.
- `fotoUrl` ausente: seção da imagem da obra é omitida, layout se
  ajusta (sem espaço em branco vazio).
- Falha inesperada na geração do PDF: retorna 500 com mensagem de erro
  (mesmo padrão de `AppError` usado no resto do app).

## Testes

- `montar_mapa_tarefas` retorna atividades completas com porcentagem e
  quantidade corretas (dado um `cronograma` de exemplo).
- `coletar_fotos_obra` inclui `cabecalho` no contexto retornado.
- `relatorio_pdf.gerar_pdf` roda sem exceção com dados de exemplo
  (incluindo caso sem `fotoUrl`, caso com foto ausente) e produz bytes
  não vazios com assinatura `%PDF`.
- Rota `/api/pdf/<zid>` retorna 404 pra zid inexistente, e retorna PDF
  válido (`Content-Type: application/pdf`) pra zid válido com dados
  cacheados.
