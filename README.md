# Auditor de Relatórios — APP Diário de Obra

Ferramenta de linha de comando que, para um relatório do sistema **Diário de Obra**:

1. **Baixa** todas as fotos do relatório para a pasta `Downloads`.
2. **Nomeia** cada foto pelo código da tarefa (`A1.jpg`; ou `A1.1.jpg`, `A1.2.jpg`… quando a tarefa tem várias fotos).
3. *(opcional, com `--ia`)* **Audita** cada foto com um modelo de IA com visão, verificando se a foto é compatível com a descrição da atividade.
4. **Gera** um `auditoria.csv` e um resumo no terminal.

> **Por padrão o script só baixa e nomeia as fotos** — não usa IA e não precisa de nenhuma chave de IA. A auditoria por IA é opcional (flag `--ia`).

Há **duas formas de usar**: a **interface web** (recomendada, com galeria de fotos) e a **linha de comando**.

## Interface web (recomendada)

1. Instale as dependências: `pip install -r requirements.txt`
2. **Dê dois cliques em `Auditor RDO.bat`** (ou rode `python app.py`).
3. O navegador abre sozinho em `http://127.0.0.1:5000`.
4. Cole o **token** e a **URL do relatório**, clique em **Carregar relatório**, confira a galeria e clique em **Baixar fotos**.

Tudo roda localmente no seu PC: o token não sai da máquina e as fotos vão para a sua pasta Downloads. A auditoria por IA é um botão opcional na própria tela.

## Instalação

Para o uso padrão (baixar + nomear) **não é preciso instalar nada** além do Python — as chamadas à API do Diário de Obra e os downloads usam a biblioteca padrão.

Opcionalmente, para a auditoria por IA e para redimensionar imagens:

```bash
pip install -r requirements.txt
```

## Configuração (variáveis de ambiente)

| Variável | Quando é necessária | Descrição |
|---|---|---|
| `DIARIODEOBRA_TOKEN` | Sempre | Token gerado em **Cadastros > Empresa > Gerar token** no sistema web. |
| `GROQ_API_KEY` | Só com `--ia` (padrão) | Chave gratuita — [console.groq.com/keys](https://console.groq.com/keys). |
| `ANTHROPIC_API_KEY` | Só com `--ia --provedor anthropic` | Chave paga — [console.anthropic.com](https://console.anthropic.com/settings/keys). |

No PowerShell (Windows):

```powershell
$env:DIARIODEOBRA_TOKEN = "seu_token"
```

## Uso pela linha de comando

Aceita a **URL completa** do relatório ou os dois IDs separados:

```bash
python auditar_relatorio.py "https://web.diariodeobra.app/#/app/obras/<obra>/relatorios/<rel>/editar"
```

ou

```bash
python auditar_relatorio.py <obra_id> <relatorio_id>
```

Exemplo:

```bash
python auditar_relatorio.py 69e62ce907797d5a0d02bd17 69e6328daa6865a6a3078bd4
```

Os IDs vêm da URL do relatório no sistema web:
`.../obras/{obra_id}/relatorios/{relatorio_id}/editar`

### Opções

| Opção | Efeito |
|---|---|
| `--forcar` | Rebaixa as fotos mesmo que já existam (por padrão é idempotente: não duplica). |
| `--ia` | Ativa a auditoria por IA (por padrão só baixa e nomeia). |
| `--provedor NOME` | Provedor de IA com `--ia`: `groq` (padrão, gratuito) ou `anthropic` (pago). |
| `--modelo NOME` | Modelo de visão específico (padrão depende do provedor). |
| `--saida PASTA` | Pasta de destino (padrão: `~/Downloads/relatorio_{numero}_{data}/`). |

Exemplo com auditoria por IA gratuita (Groq):

```bash
python auditar_relatorio.py <obra_id> <relatorio_id> --ia
```

## Saída

- Pasta `~/Downloads/relatorio_{numero}_{data}/` com as fotos nomeadas por código.
- `auditoria.csv` (colunas: `codigo; arquivo; descricao; veredito; motivo`) — separador `;` e UTF-8 com BOM, abre direto no Excel em português.
- Resumo no terminal com a contagem por veredito e a lista das fotos DIVERGENTES/INCONCLUSIVAS.

## Sobre os vereditos

- **COMPATIVEL** — a foto mostra, de forma plausível, o serviço/local descrito.
- **DIVERGENTE** — a foto claramente não bate com a descrição (revisar manualmente).
- **INCONCLUSIVO** — a foto está ruim demais para avaliar (escura, borrada, sem enquadramento).

## Custo

O modelo padrão é `claude-opus-5` (mais capaz). Em relatórios com muitas fotos, para reduzir custo defina um modelo mais barato:

```bash
python auditar_relatorio.py <obra_id> <relatorio_id> --modelo claude-sonnet-5
```

## Segurança

Este app roda **100% localmente** (`127.0.0.1`, sem acesso pela rede) e é de uso
individual. Por isso, boa parte de um checklist de segurança genérico para
aplicações web não se aplica aqui:

- **Sem banco de dados** → não há RLS, criptografia de dados em banco, mass
  assignment ou queries SQL para parametrizar.
- **Sem sistema de login/usuários** → não há senha para fazer hash, nem
  sessão/cookie para proteger.
- **Servidor não exposto na internet** → rate limit, bot protection e HTTPS
  forçado não fazem sentido para um processo que só escuta em `127.0.0.1`.

O que **é** relevante e já está tratado:

- O token do Diário de Obra (e a chave da Groq, se configurada) ficam em
  `config.json`, salvo **apenas na sua máquina**. Esse arquivo está no
  `.gitignore` — **nunca o adicione a um commit**.
- O proxy de imagens (`/api/img`) e os endpoints de arquivo
  (`/api/abrir-pasta`, `/api/csv`) validam a entrada: só aceitam URLs
  `https` que não apontem para IPs internos/privados, e só servem caminhos
  dentro da sua pasta `Downloads`.
- Antes de publicar este projeto em um repositório Git, rode
  `pip-audit -r requirements.txt` (ou equivalente) para checar dependências
  com vulnerabilidades conhecidas, e confira que `config.json` não está
  rastreado (`git status` não deve listá-lo).
