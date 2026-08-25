# Auditor de Relatórios — APP Diário de Obra

## Sobre o projeto

Em obras que usam o app **Diário de Obra** para registrar relatórios diários
(RDO), cada tarefa do relatório vem com fotos anexadas que deveriam mostrar o
serviço descrito sendo executado. Conferir isso manualmente — abrir cada
relatório, baixar cada foto, comparar com a descrição da tarefa — é lento e
repetitivo em obras com muitas tarefas e fotos.

O **Auditor de Relatórios** automatiza essa conferência: dado um relatório
(ou uma obra inteira), ele baixa e organiza as fotos por código de tarefa e,
opcionalmente, usa um modelo de IA com visão para avaliar se cada foto é
compatível com a descrição da atividade — sinalizando as que precisam de
revisão manual. Rodando localmente, tudo fica na sua máquina; rodando
hospedado (ver "Deploy" abaixo), o token é enviado ao servidor a cada
requisição, mas nunca é salvo nele.

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

O token fica salvo só no seu navegador (nunca no servidor), e ao final as fotos + auditoria.csv são entregues como um .zip para download. A auditoria por IA é um botão opcional na própria tela.

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

Rodando localmente (`python app.py`, sem `APP_USERS` definida), o app é
100% pessoal: escuta só em `127.0.0.1`, sem login, sem exposição de rede.
Boa parte de um checklist de segurança genérico para aplicações web não se
aplica a esse modo — sem banco de dados (não há RLS, criptografia de dados
em banco, mass assignment ou queries SQL para parametrizar).

Rodando hospedado (Render, com `APP_USERS` definida — ver "Deploy" abaixo),
uma tela de login pede **usuário e senha** (um login por pessoa, não uma
senha única compartilhada), com bloqueio por tentativas erradas repetidas
(rate limit simples). As senhas ficam salvas só como **hash** (nunca em
texto puro) — mesmo quem tiver acesso à variável de ambiente no Render não
consegue recuperar a senha original. A sessão expira automaticamente após
30 minutos sem uso (pedindo login de novo) e também não sobrevive a fechar
o navegador — não é um cookie "lembrar de mim". Em ambos os casos:

- O token de cada pessoa nunca é salvo no servidor — fica só no
  `localStorage` do navegador dela (veja "esquecer token salvo" na tela).
- O proxy de imagens (`/api/img`) valida a entrada: só aceita URLs `https`
  que não apontem para IPs internos/privados (evita SSRF).
- As fotos processadas e o `auditoria.csv` são entregues como um `.zip`
  baixado pelo navegador — nenhum arquivo fica salvo em disco além de um
  diretório temporário por requisição, removido logo após gerar o zip.
- Antes de publicar este projeto (ou uma nova versão) em um repositório
  Git, rode `pip-audit -r requirements.txt` (ou equivalente) para checar
  dependências com vulnerabilidades conhecidas.

## Deploy (Render.com)

1. Suba este repositório no GitHub (se ainda não estiver).
2. Em [render.com](https://render.com), crie um **Web Service** novo,
   conectado a este repositório.
3. Configuração do serviço:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** deixe em branco (o Render lê o `Procfile`
     automaticamente) ou use `gunicorn app:app --workers 1 --threads 4`.
4. Em **Environment**, adicione as variáveis:
   - `APP_USERS` — um JSON com um usuário por pessoa da equipe, cada um com
     o **hash** da senha (nunca a senha em texto puro). Gere o hash de cada
     senha rodando, para cada pessoa:
     ```bash
     python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('a_senha_da_pessoa'))"
     ```
     e monte o JSON com o resultado, por exemplo:
     ```json
     {"joao": "scrypt:32768:8:1$...", "maria": "scrypt:32768:8:1$..."}
     ```
   - `SECRET_KEY` — qualquer string longa e aleatória (ex.: gerada com
     `python -c "import secrets; print(secrets.token_hex(32))"`).
5. Deploy. O Render expõe uma URL `https://` própria — HTTPS já vem pronto.
6. Compartilhe a URL com a equipe, e a senha combinada com cada pessoa
   individualmente (não precisa ser a mesma senha pra todo mundo). Cada
   pessoa cola seu próprio token do Diário de Obra (e sua própria chave de
   IA, se for usar auditoria) — nada disso fica salvo no servidor.

**Importante:** o serviço precisa rodar com um único worker
(`--workers 1`, já configurado no `Procfile`) — o cache de relatórios, o
controle de tentativas de login e os zips prontos para download vivem em
memória, e workers diferentes não veem a memória um do outro.

**Importante:** `APP_USERS` só funciona com segurança se o app rodar
atrás de HTTPS com um proxy reverso confiável na frente — exatamente o que
o Render (o caminho de deploy documentado acima) já fornece por padrão.
Rodar este app com `PORT` definida mas sem um proxy real terminando TLS na
frente não é suportado: o cookie de sessão é marcado `Secure` (só trafega
em HTTPS) e vira um loop de redirecionamento silencioso para `/login`
mesmo com a senha certa, e o rate limit do login passa a confiar
cegamente no cabeçalho `X-Forwarded-For`, que qualquer requisição pode
forjar sem um proxy real filtrando-o.
