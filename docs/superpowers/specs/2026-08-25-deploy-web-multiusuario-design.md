# Deploy web multiusuário — Auditor RDO

Data: 2026-08-25

## Contexto

Hoje o Auditor RDO é um app Flask pensado para uso **local, single-user**:
roda em `127.0.0.1`, guarda o token do Diário de Obra em `config.json` no
disco do servidor, salva fotos/CSV em `~/Downloads` da máquina onde roda, e
usa `os.startfile` (Windows-only) para abrir a pasta de destino.

O usuário quer disponibilizar o app na web para que colegas de confiança da
mesma empresa (equipe pequena e conhecida, não público em geral) também
possam usá-lo, cada um com seu próprio token do Diário de Obra.

Hospedar o código como está seria inseguro e quebrado: `config.json` é um
arquivo único compartilhado por todos os processos do servidor — o token de
uma pessoa vazaria/sobrescreveria o de outra — e escrever em `~/Downloads` do
servidor não tem sentido nenhum fora de uma máquina pessoal.

## Decisões já tomadas (via perguntas ao usuário)

- **Público:** equipe pequena e de confiança, não é público geral.
- **Entrega de arquivos:** baixar um `.zip` pelo navegador (fotos + CSV),
  substituindo a pasta local + botão "abrir pasta".
- **Chave de IA (Groq/Anthropic):** cada pessoa usa a própria chave, colada
  no formulário — sem chave compartilhada pelo dono do app.
- **Proteção de acesso:** uma senha única compartilhada pela equipe.
- **Hospedagem:** sem preferência definida — recomendado Render.com (plano
  gratuito, deploy direto do GitHub, HTTPS automático).

## Arquitetura

Um único `app.py`/Flask continua servindo tanto o uso local quanto o
hospedado — sem branch de código por ambiente, só configuração via variáveis
de ambiente:

- **Local** (como hoje): `python app.py` — sem `APP_PASSWORD` definida, roda
  sem tela de login, exatamente como hoje.
- **Hospedado** (Render): `gunicorn app:app`, com `APP_PASSWORD` e
  `SECRET_KEY` definidas no painel do Render (nunca commitadas).

## Componentes

### 1. Acesso por senha compartilhada

- Env vars novas: `APP_PASSWORD` (senha da equipe) e `SECRET_KEY` (assina o
  cookie de sessão do Flask).
- Rota `GET/POST /login` com formulário simples (`templates/login.html`).
  No POST, compara a senha enviada com `APP_PASSWORD`; se bater, marca
  `session["autenticado"] = True` e redireciona pra `/`.
- `@app.before_request` bloqueia todas as rotas exceto `/login` e estáticos
  quando `APP_PASSWORD` está definida e a sessão não está autenticada
  (redireciona pra `/login`). Quando `APP_PASSWORD` não está definida
  (uso local), o guard não faz nada — comportamento atual preservado.
- Rate limit simples no `/login`: contador em memória por IP (dict com
  timestamps), bloqueia por alguns minutos após ~10 tentativas erradas.
  Não é rate limit distribuído/persistente — suficiente para um app de
  processo único protegendo uma senha de equipe, não uma API pública.
- Cookie de sessão: `SESSION_COOKIE_HTTPONLY=True` (padrão do Flask),
  `SESSION_COOKIE_SAMESITE="Lax"`, e `SESSION_COOKIE_SECURE=True` quando
  detectar HTTPS (via `X-Forwarded-Proto`, que o Render define).

### 2. Token de cada pessoa fica só no navegador

- Remove `_ler_config`/`_salvar_config`, o arquivo `config.json` e as rotas
  `/api/config`, `/api/config` (POST) e `/api/config/limpar`.
- `templates/index.html`: a função `salvarConfig()` e o prefill no load
  passam a usar `localStorage.setItem("token", ...)` /
  `localStorage.getItem("token")` em vez de `fetch("/api/config")`. Mesma UX
  ("lembrar neste PC/navegador"), mas o token nunca mais toca disco do
  servidor.
- `.gitignore`: a entrada `config.json` deixa de ser necessária (mas não faz
  mal mantê-la, caso algum resquício local ainda exista).

### 3. Entrega dos arquivos via .zip

- `/api/processar` passa a gravar fotos + CSV num diretório temporário por
  requisição (`tempfile.mkdtemp(prefix="auditor_rdo_")`), não mais em
  `~/Downloads`.
- Ao terminar (evento `"fim"` do stream ndjson), o servidor zipa o
  diretório temporário e devolve, no próprio evento, um identificador de
  download (ex.: nome do arquivo temporário do zip). Uma nova rota
  `GET /api/zip/<id>` serve esse zip com `send_file(..., as_attachment=True)`
  e agenda a remoção do diretório temporário logo em seguida (ou via
  limpeza por TTL, o que for mais simples de implementar corretamente).
- Remove as rotas `/api/abrir-pasta` e `/api/csv` (aceitavam caminho de
  arquivo vindo do cliente) e o helper `_caminho_seguro` que as validava —
  deixam de existir, então a validação também deixa de ser necessária.
- `templates/index.html`: troca o botão "abrir pasta" por "baixar .zip"
  (`location.href = "/api/zip/" + id`), remove o botão de CSV avulso (o CSV
  já vai dentro do zip).

### 4. O que não muda

- Validação SSRF em `/api/img` (`_url_de_imagem_segura`) — continua igual,
  e passa a valer mais ainda por o app estar exposto na internet.
- Cabeçalhos de segurança (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`).
- Fluxo de auditoria por IA — cada pessoa cola sua própria chave, sem
  mudança de comportamento.
- `_CACHE` em memória (TTL de 10 min, chaveado pelo alvo/relatório) —
  continua como está; múltiplas pessoas olhando o mesmo relatório vêem o
  cache um do outro, mas isso é dado da obra (não é segredo entre colegas
  da mesma empresa), então não é um problema de isolamento neste contexto.

### 5. Deploy

- `requirements.txt`: adiciona `gunicorn` (só necessário no servidor Linux;
  não atrapalha o uso local no Windows).
- `Procfile` (novo): `web: gunicorn app:app`.
- `README.md`: nova seção "Deploy" explicando como configurar no Render
  (conectar o repo, definir `APP_PASSWORD` e `SECRET_KEY` como env vars,
  build command `pip install -r requirements.txt`, start command via
  `Procfile`).

## Erros e casos de borda

- `APP_PASSWORD` não definida → sem tela de login (uso local intacto).
- Zip: se a geração falhar (disco cheio, permissão), o evento `"fim"` deve
  reportar erro em vez de um id de download inválido.
- Diretório temporário: precisa ser limpo mesmo se a pessoa nunca baixar o
  zip (evitar acúmulo de lixo no disco do servidor ao longo do tempo) —
  limpeza por TTL (ex.: remover diretórios com mais de 1h) resolve isso de
  forma simples, rodando a checagem a cada novo request de processamento.

## Teste

Sem suíte automatizada no projeto. Validação manual:
1. Local, sem `APP_PASSWORD`: app funciona exatamente como hoje (sem login),
   token lembrado via localStorage, zip baixa corretamente.
2. Local, com `APP_PASSWORD` definida (simulando produção): tela de login
   aparece, senha errada é rejeitada (e rate-limitada após várias
   tentativas), senha certa libera o app.
3. Zip contém as fotos nomeadas corretamente + `auditoria.csv`.
4. `/api/abrir-pasta` e `/api/csv` não existem mais (404).
5. Deploy real no Render como validação final ponta a ponta.

## Fora de escopo (YAGNI)

- Contas individuais por pessoa (login/senha por usuário, com hash e DB) —
  não pedido; senha única de equipe é suficiente pra "colegas de confiança".
- Rate limit geral em todos os endpoints — só o `/login` recebe, por ser o
  único ponto de força bruta relevante numa equipe pequena.
- Chave de IA compartilhada pelo dono do app — decidido que cada pessoa usa
  a própria.
