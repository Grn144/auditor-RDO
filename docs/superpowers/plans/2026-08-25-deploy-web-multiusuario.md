# Deploy web multiusuário — Auditor RDO — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tornar o Auditor RDO utilizável por uma equipe pequena e de confiança na web (Render.com), sem quebrar o uso local atual, protegendo com senha compartilhada, tirando o token do disco do servidor e trocando a entrega de arquivos por um `.zip` baixado pelo navegador.

**Architecture:** O mesmo `app.py`/Flask serve local e hospedado — sem branch de código por ambiente, só configuração via variáveis de ambiente (`APP_PASSWORD`, `SECRET_KEY`, `PORT`). Login por senha única fica atrás de um `@app.before_request`; o token de cada pessoa passa a viver só no `localStorage` do navegador; o processamento grava num diretório temporário por requisição e devolve um `.zip` para download.

**Tech Stack:** Python 3 / Flask (já em uso), `pytest` + `flask.testing` (test client, sem dependências novas de teste), `gunicorn` (WSGI de produção), Render.com (hospedagem).

**Spec:** `docs/superpowers/specs/2026-08-25-deploy-web-multiusuario-design.md`

## Global Constraints

- Sem `APP_PASSWORD` definida, o comportamento local atual (`python app.py`, sem login, abre o navegador em `127.0.0.1`) fica **idêntico** ao de hoje.
- `APP_PASSWORD` e `SECRET_KEY` só existem como variáveis de ambiente no host — nunca hardcoded, nunca commitadas.
- O estado em memória (`_CACHE`, `_zips`, `_login_tentativas`) é por processo, não compartilhado entre processos — por isso o deploy em produção **precisa** rodar com um único worker (`gunicorn app:app --workers 1`). Não introduzir Redis/DB só para isso (YAGNI — equipe pequena).
- Nenhum banco de dados novo, nenhuma conta individual por pessoa (fora de escopo da spec).
- Todo teste novo usa `pytest` + `flask.app.test_client()` — sem mocks de rede reais; testes que dependeriam da API do Diário de Obra ficam como verificação manual (documentada em cada task).

---

### Task 1: Login por senha compartilhada (com rate limit)

**Files:**
- Modify: `app.py` (imports, novo bloco de auth, `@app.before_request`, rota `/login`)
- Create: `templates/login.html`
- Create: `tests/conftest.py`
- Create: `tests/test_app.py`
- Modify: `requirements.txt` (adiciona `pytest`)

**Interfaces:**
- Produces: `APP_PASSWORD: str | None` (global do módulo), `_LOGIN_MAX_TENTATIVAS: int`, `_LOGIN_JANELA: int`, `_ip_bloqueado(ip: str) -> bool`, `_registrar_tentativa_falha(ip: str) -> None`, rota `login` (endpoint Flask `"login"`), `import os` no topo de `app.py` (tasks seguintes podem usar `os.environ`).

- [ ] **Step 1: Criar `requirements.txt` com `pytest` e criar os arquivos de teste (que ainda vão falhar)**

Adicionar ao final de `requirements.txt`:

```
# Só para rodar os testes (pytest tests/) - não precisa em produção nem para uso local normal.
pytest>=8.0.0
```

Criar `tests/conftest.py`:

```python
import sys
from pathlib import Path

# Garante que `import app` e `import auditar_relatorio` funcionem
# independente de como o pytest for invocado.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Criar `tests/test_app.py`:

```python
import importlib
import time

import pytest


@pytest.fixture
def app_module(monkeypatch):
    """Recarrega app.py com env vars controladas para cada teste."""
    import app as _app_module

    def _carregar(app_password=None, secret_key="teste-secret"):
        if app_password is None:
            monkeypatch.delenv("APP_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("APP_PASSWORD", app_password)
        monkeypatch.setenv("SECRET_KEY", secret_key)
        importlib.reload(_app_module)
        _app_module.app.testing = True
        return _app_module

    return _carregar


def test_sem_senha_configurada_nao_exige_login(app_module):
    mod = app_module(app_password=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/")
    assert resp.status_code == 200


def test_com_senha_configurada_redireciona_para_login(app_module):
    mod = app_module(app_password="segredo123")
    cliente = mod.app.test_client()
    resp = cliente.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_com_senha_certa_libera_acesso(app_module):
    mod = app_module(app_password="segredo123")
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"senha": "segredo123"}, follow_redirects=True)
    assert resp.status_code == 200
    resp2 = cliente.get("/")
    assert resp2.status_code == 200


def test_login_com_senha_errada_nao_libera(app_module):
    mod = app_module(app_password="segredo123")
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"senha": "errada"})
    assert "incorreta" in resp.get_data(as_text=True).lower()
    resp2 = cliente.get("/", follow_redirects=False)
    assert resp2.status_code == 302


def test_bloqueia_apos_muitas_tentativas_erradas(app_module):
    mod = app_module(app_password="segredo123")
    cliente = mod.app.test_client()
    for _ in range(mod._LOGIN_MAX_TENTATIVAS):
        cliente.post("/login", data={"senha": "errada"})
    resp = cliente.post("/login", data={"senha": "segredo123"})
    assert "aguarde" in resp.get_data(as_text=True).lower()
```

- [ ] **Step 2: Rodar os testes e confirmar que falham (a rota `/login` ainda não existe)**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL em todos os testes que dependem de `/login` (404 em vez de 200/302), e os dois primeiros testes também podem falhar porque `APP_PASSWORD`/before_request ainda não existem.

- [ ] **Step 3: Implementar o login no `app.py`**

No topo do arquivo, adicionar aos imports existentes:

```python
import os
import secrets
```

Trocar a linha de import do Flask para incluir `redirect, session, url_for`:

```python
from flask import (Flask, Response, jsonify, redirect, request,
                   render_template, send_file, session,
                   stream_with_context, url_for)
```

Logo depois de `app = Flask(__name__)`, adicionar:

```python
APP_PASSWORD = os.environ.get("APP_PASSWORD")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(APP_PASSWORD),
)

_LOGIN_JANELA = 15 * 60  # 15 minutos
_LOGIN_MAX_TENTATIVAS = 10
_login_tentativas: dict[str, list[float]] = {}


def _registrar_tentativa_falha(ip: str) -> None:
    agora = time.time()
    tentativas = [t for t in _login_tentativas.get(ip, []) if agora - t < _LOGIN_JANELA]
    tentativas.append(agora)
    _login_tentativas[ip] = tentativas


def _ip_bloqueado(ip: str) -> bool:
    agora = time.time()
    tentativas = [t for t in _login_tentativas.get(ip, []) if agora - t < _LOGIN_JANELA]
    _login_tentativas[ip] = tentativas
    return len(tentativas) >= _LOGIN_MAX_TENTATIVAS
```

Antes da rota `@app.route("/")`, adicionar o guard e a rota de login:

```python
@app.before_request
def _exigir_login():
    if not APP_PASSWORD:
        return None
    if request.endpoint in ("login", "static") or session.get("autenticado"):
        return None
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        ip = request.remote_addr or "desconhecido"
        if _ip_bloqueado(ip):
            erro = "Muitas tentativas erradas. Aguarde alguns minutos."
        elif request.form.get("senha") == APP_PASSWORD:
            session["autenticado"] = True
            return redirect(url_for("index"))
        else:
            _registrar_tentativa_falha(ip)
            erro = "Senha incorreta."
    return render_template("login.html", erro=erro)
```

Criar `templates/login.html`:

```html
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Auditor RDO</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;
    font-family:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
    background:#f4f6f9;color:#1a2230}
  .card{background:#fff;border:1px solid #dce3ec;border-radius:14px;
    box-shadow:0 1px 3px rgba(16,24,40,.08),0 8px 24px rgba(16,24,40,.06);
    padding:28px;width:320px}
  h1{font-size:18px;margin:0 0 18px}
  input{width:100%;padding:11px 13px;font-size:14px;background:#eef2f7;
    border:1px solid #dce3ec;border-radius:10px;color:#1a2230;margin-bottom:14px;box-sizing:border-box}
  button{width:100%;padding:12px;font-size:14px;font-weight:650;border:none;
    border-radius:10px;background:#1f6feb;color:#fff;cursor:pointer}
  .erro{background:#fee2e2;color:#b91c1c;border-radius:8px;padding:9px 12px;
    font-size:13px;margin-bottom:14px}
</style>
</head>
<body>
  <form class="card" method="post">
    <h1>🏗️ Auditor RDO</h1>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <input type="password" name="senha" placeholder="Senha da equipe" autofocus required>
    <button type="submit">Entrar</button>
  </form>
</body>
</html>
```

- [ ] **Step 4: Rodar os testes de novo e confirmar que passam**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS em todos os 5 testes.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/login.html tests/conftest.py tests/test_app.py requirements.txt
git commit -m "feat: login por senha compartilhada com rate limit"
```

---

### Task 2: Token de cada pessoa só no navegador (remove `config.json` do servidor)

**Files:**
- Modify: `app.py` (remove `CONFIG_PATH`, `_ler_config`, `_salvar_config`, rotas `/api/config*`)
- Modify: `templates/index.html` (troca chamadas a `/api/config*` por `localStorage`)
- Modify: `tests/test_app.py` (novo teste)

**Interfaces:**
- Consumes: fixture `app_module` de `tests/test_app.py` (Task 1).
- Produces: nada que outras tasks dependam (só remove código).

- [ ] **Step 1: Adicionar o teste que confirma a remoção das rotas**

No final de `tests/test_app.py`, adicionar:

```python
def test_rotas_de_config_nao_existem_mais(app_module):
    mod = app_module(app_password=None)
    cliente = mod.app.test_client()
    assert cliente.get("/api/config").status_code == 404
    assert cliente.post("/api/config", json={}).status_code == 404
    assert cliente.post("/api/config/limpar").status_code == 404
```

- [ ] **Step 2: Rodar o teste e confirmar que falha (as rotas ainda existem, retornam 200)**

Run: `python -m pytest tests/test_app.py::test_rotas_de_config_nao_existem_mais -v`
Expected: FAIL (recebe 200 em vez de 404).

- [ ] **Step 3: Remover a persistência server-side em `app.py`**

Remover a linha `CONFIG_PATH = Path(__file__).with_name("config.json")`.

Remover todo o bloco:

```python
# ----------------------------------------------------------------------------
# Persistência simples do token (opcional, "lembrar neste PC")
# ----------------------------------------------------------------------------

def _ler_config() -> dict:
    ...

def _salvar_config(dados: dict) -> None:
    ...
```

Remover as três rotas `/api/config` (GET), `/api/config` (POST) e `/api/config/limpar`.

- [ ] **Step 4: Trocar a persistência no front-end (`templates/index.html`)**

Trocar o bloco:

```js
// prefill config (token é sempre lembrado)
fetch("/api/config").then(r=>r.json()).then(c=>{
  if(c.token){ $("#token").value=c.token; }
  if(c.groq_key){ $("#chaveIA").value=c.groq_key; }
}).catch(()=>{});

function salvarConfig(){
  fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({token:$("#token").value.trim(), groq_key:$("#chaveIA").value.trim()})});
}
// salva ao terminar de digitar/colar o token ou a chave
$("#token").addEventListener("change", salvarConfig);
$("#chaveIA").addEventListener("change", salvarConfig);
$("#esquecer").onclick = (e) => { e.preventDefault();
  fetch("/api/config/limpar",{method:"POST"}).then(()=>{ $("#token").value=""; toast("Token salvo foi apagado deste PC."); });
};
```

por:

```js
// prefill (token é lembrado neste navegador, via localStorage)
try{
  const tokenSalvo = localStorage.getItem("auditor_rdo_token");
  const chaveSalva = localStorage.getItem("auditor_rdo_groq_key");
  if(tokenSalvo) $("#token").value = tokenSalvo;
  if(chaveSalva) $("#chaveIA").value = chaveSalva;
}catch(e){}

function salvarConfig(){
  try{
    localStorage.setItem("auditor_rdo_token", $("#token").value.trim());
    localStorage.setItem("auditor_rdo_groq_key", $("#chaveIA").value.trim());
  }catch(e){}
}
$("#token").addEventListener("change", salvarConfig);
$("#chaveIA").addEventListener("change", salvarConfig);
$("#esquecer").onclick = (e) => { e.preventDefault();
  try{ localStorage.removeItem("auditor_rdo_token"); }catch(err){}
  $("#token").value=""; toast("Token salvo foi apagado deste navegador.");
};
```

E atualizar o texto de dica, trocando:

```html
<div class="hint">🔒 O token é salvo neste computador e preenchido automaticamente. <a href="https://web.diariodeobra.app" target="_blank" rel="noopener">Abrir o sistema</a> · <a href="#" id="esquecer">esquecer token salvo</a></div>
```

por:

```html
<div class="hint">🔒 O token é salvo neste navegador e preenchido automaticamente. <a href="https://web.diariodeobra.app" target="_blank" rel="noopener">Abrir o sistema</a> · <a href="#" id="esquecer">esquecer token salvo</a></div>
```

- [ ] **Step 5: Rodar os testes de novo e confirmar que passam**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS em todos os testes (agora 6).

- [ ] **Step 6: Verificação manual no navegador**

Rodar `python app.py`, abrir `http://127.0.0.1:5000`, colar um token qualquer no campo, dar Tab (dispara o `change`), recarregar a página e confirmar que o campo continua preenchido. Clicar em "esquecer token salvo" e confirmar que o campo esvazia.

- [ ] **Step 7: Commit**

```bash
git add app.py templates/index.html tests/test_app.py
git commit -m "feat: token do usuário passa a viver só no navegador (localStorage), remove config.json do servidor"
```

---

### Task 3: Entrega dos arquivos via `.zip` (substitui pasta local + botão "abrir pasta")

**Files:**
- Modify: `app.py` (`_coletar`/`_coletar_cacheado`, `api_carregar`, `api_processar`, novas rotas/helpers de zip, remove `_caminho_seguro`/`PASTA_DOWNLOADS`/`_destino_de`/`/api/abrir-pasta`/`/api/csv`)
- Modify: `templates/index.html` (botão "baixar .zip", remove "abrir pasta"/"baixar CSV"/checkbox "rebaixar", remove exibição de destino)
- Modify: `tests/test_app.py` (novos testes)

**Interfaces:**
- Consumes: fixture `app_module` (Task 1).
- Produces: `_zips: dict[str, dict]`, `_ZIP_TTL: int`, `_zipar_diretorio(pasta_temp: Path, nome_arquivo: str) -> str` (retorna o id do zip), `_limpar_zips_antigos() -> None`, rota `GET /api/zip/<zid>`. `_coletar(...)` e `_coletar_cacheado(...)` agora retornam `(fotos, info)` — 2-tupla, não mais 3 — e `info` ganha a chave `"nome_arquivo"`.

- [ ] **Step 1: Adicionar os testes (vão falhar — as rotas/helpers ainda não existem)**

No final de `tests/test_app.py`, adicionar (e `import time` já está no topo do arquivo, de Task 1):

```python
def test_zip_helpers_e_rota_download(app_module, tmp_path):
    mod = app_module(app_password=None)
    pasta = tmp_path / "fotos_teste"
    pasta.mkdir()
    (pasta / "A1.jpg").write_bytes(b"fake-jpg")
    (pasta / "auditoria.csv").write_text("codigo;arquivo\nA1;A1.jpg", encoding="utf-8")

    zid = mod._zipar_diretorio(pasta, "relatorio_teste")

    assert not pasta.exists()  # pasta de origem foi removida depois de zipar
    cliente = mod.app.test_client()
    resp = cliente.get(f"/api/zip/{zid}")
    assert resp.status_code == 200
    assert "relatorio_teste.zip" in resp.headers.get("Content-Disposition", "")


def test_zip_id_desconhecido_da_404(app_module):
    mod = app_module(app_password=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/api/zip/nao-existe")
    assert resp.status_code == 404


def test_limpar_zips_antigos_remove_expirados(app_module, tmp_path):
    mod = app_module(app_password=None)
    arquivo_zip = tmp_path / "velho.zip"
    arquivo_zip.write_bytes(b"conteudo")
    zid = "velho"
    mod._zips[zid] = {"caminho": arquivo_zip, "nome": "velho.zip",
                       "criado_em": time.time() - mod._ZIP_TTL - 10}
    mod._limpar_zips_antigos()
    assert zid not in mod._zips
    assert not arquivo_zip.exists()


def test_rotas_antigas_de_arquivo_nao_existem_mais(app_module):
    mod = app_module(app_password=None)
    cliente = mod.app.test_client()
    assert cliente.post("/api/abrir-pasta", json={}).status_code == 404
    assert cliente.get("/api/csv").status_code == 404
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL nos 4 novos testes (`AttributeError` em `_zipar_diretorio`/`_zips`/`_limpar_zips_antigos`, e as rotas antigas ainda existindo).

- [ ] **Step 3: Adicionar os imports novos no topo de `app.py`**

```python
import shutil
import tempfile
import uuid
```

- [ ] **Step 4: Reescrever `_coletar`/`_coletar_cacheado` para não usar mais `~/Downloads`**

No arquivo atual, a ordem é `_coletar` → `_coletar_cacheado` → `_foto_dict` →
`_destino_de`. Trocar **só** `_coletar` e `_coletar_cacheado` (deixando
`_foto_dict` como está, sem tocar nele) por:

```python
def _coletar(token: str, alvo: str, progresso=None):
    """Resolve o alvo (relatório ou obra) e coleta as fotos.
    Retorna (fotos, info)."""
    modo, obra_id, rel_id = core.resolver_alvo(alvo)
    client = core.DiarioObraClient(token)
    if modo == "obra":
        fotos, ctx = core.coletar_fotos_obra(client, obra_id, progresso)
        info = {"modo": "obra", "titulo": ctx.get("nome", obra_id),
                "subtitulo": "Obra completa (todas as tarefas)",
                "total": len(fotos), "grupos": ctx.get("grupos", 0),
                "nome_arquivo": core._sanitizar_pasta(f"obra {ctx.get('nome', obra_id)}")}
    else:
        mapa = core.montar_mapa_tarefas(client, obra_id)
        fotos, rel = core.coletar_fotos(client, obra_id, rel_id, mapa)
        info = {"modo": "relatorio",
                "titulo": f"Relatório nº {rel.get('numero', '?')}",
                "subtitulo": rel.get("data", ""),
                "total": len(fotos),
                "grupos": len({f.subpasta for f in fotos}),
                "nome_arquivo": f"relatorio_{rel.get('numero', 'x')}_{core.sanitizar(str(rel.get('data', '')))}"}
    return fotos, info


def _coletar_cacheado(token: str, alvo: str, progresso=None):
    """Como _coletar, mas reaproveita um resultado recente do mesmo alvo."""
    chave = (alvo or "").strip()
    ent = _CACHE.get(chave)
    if ent and (time.time() - ent["ts"] < _CACHE_TTL):
        return ent["fotos"], ent["info"]
    fotos, info = _coletar(token, alvo, progresso)
    _CACHE[chave] = {"fotos": fotos, "info": info, "ts": time.time()}
    return fotos, info
```

Depois, remover a função `_destino_de` inteira (ela fica logo depois de
`_foto_dict`, já não tem mais nenhum código chamando ela):

```python
def _destino_de(rel: dict) -> Path:
    pasta = f"relatorio_{rel.get('numero', 'x')}_{core.sanitizar(str(rel.get('data', '')))}"
    return Path.home() / "Downloads" / pasta
```

- [ ] **Step 5: Remover a validação de caminho (não é mais necessária)**

Remover o bloco inteiro:

```python
# ----------------------------------------------------------------------------
# Validação de inputs (SSRF / path traversal)
# ----------------------------------------------------------------------------

PASTA_DOWNLOADS = (Path.home() / "Downloads").resolve()


def _caminho_seguro(bruto: str) -> Path | None:
    ...
```

Mantendo apenas `_url_de_imagem_segura` (que continua sendo usada pelo `/api/img`) — ajustar o comentário do bloco restante para `# Validação de inputs (SSRF na imagem proxied)`.

- [ ] **Step 6: Adicionar os helpers e a rota de zip**

Logo depois de `_url_de_imagem_segura`, adicionar:

```python
# ----------------------------------------------------------------------------
# Entrega dos arquivos via .zip (cada processamento grava num diretório
# temporário; a pessoa baixa o zip resultante pelo navegador)
# ----------------------------------------------------------------------------

_ZIP_TTL = 3600  # 1 hora
_zips: dict[str, dict] = {}  # id -> {"caminho": Path, "nome": str, "criado_em": float}


def _zipar_diretorio(pasta_temp: Path, nome_arquivo: str) -> str:
    """Zipa o conteúdo de `pasta_temp`, apaga a pasta de origem e registra
    o zip resultante para download. Retorna o id do zip."""
    base = Path(tempfile.gettempdir()) / f"auditor_rdo_{uuid.uuid4().hex}"
    caminho_zip = Path(shutil.make_archive(str(base), "zip", root_dir=str(pasta_temp)))
    shutil.rmtree(pasta_temp, ignore_errors=True)
    zid = uuid.uuid4().hex
    _zips[zid] = {"caminho": caminho_zip, "nome": f"{nome_arquivo}.zip",
                  "criado_em": time.time()}
    return zid


def _limpar_zips_antigos() -> None:
    agora = time.time()
    for zid in [z for z, info in _zips.items() if agora - info["criado_em"] > _ZIP_TTL]:
        info = _zips.pop(zid)
        Path(info["caminho"]).unlink(missing_ok=True)
```

- [ ] **Step 7: Atualizar `api_carregar` (remove `destino` da resposta)**

Trocar:

```python
    try:
        fotos, info, destino = _coletar_cacheado(token, body.get("alvo", ""))
    except core.AppError as e:
```

por:

```python
    try:
        fotos, info = _coletar_cacheado(token, body.get("alvo", ""))
    except core.AppError as e:
```

E remover a linha `"destino": str(destino),` do `jsonify({...})` que segue.

- [ ] **Step 8: Reescrever `api_processar` para gravar em diretório temporário e devolver um zip**

Substituir a função inteira `api_processar` por:

```python
@app.route("/api/processar", methods=["POST"])
def api_processar():
    body = request.get_json(force=True)
    token = (body.get("token") or "").strip()
    alvo = body.get("alvo", "")
    usar_ia = bool(body.get("ia"))
    provedor = body.get("provedor", "groq")
    modelo = body.get("modelo") or core.DEFAULT_MODELS.get(provedor)
    chave_ia = (body.get("chave_ia") or "").strip()

    def evento(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False) + "\n"

    @stream_with_context
    def gerar():
        pasta_temp = None
        try:
            if not token:
                raise core.AppError("Informe o token do Diário de Obra.")
            yield evento({"tipo": "log", "msg": "Lendo tarefas e localizando fotos..."})
            fotos, info = _coletar_cacheado(token, alvo)
            _limpar_zips_antigos()
            pasta_temp = Path(tempfile.mkdtemp(prefix="auditor_rdo_"))
            total = len(fotos)
            yield evento({"tipo": "inicio", "total": total, "com_ia": usar_ia})

            auditor = None
            if usar_ia:
                auditor = core.criar_auditor(provedor, modelo, chave_ia)

            for i, foto in enumerate(fotos, start=1):
                pasta = pasta_temp / foto.subpasta if foto.subpasta else pasta_temp
                pasta.mkdir(parents=True, exist_ok=True)
                caminho = pasta / foto.nome_arquivo
                foto.caminho = caminho
                status = "baixado"
                try:
                    core._baixar_arquivo(foto.url, caminho)
                except Exception as e:  # noqa: BLE001
                    foto.caminho = None
                    status = "erro"
                    yield evento({"tipo": "foto", "i": i, "total": total,
                                  "uid": f"{foto.subpasta}/{foto.nome_arquivo}",
                                  "codigo": foto.codigo, "nome": foto.nome_arquivo,
                                  "status": "erro", "detalhe": str(e)})
                    continue

                uid = f"{foto.subpasta}/{foto.nome_arquivo}"
                item = {"tipo": "foto", "i": i, "total": total, "uid": uid,
                        "codigo": foto.codigo, "nome": foto.nome_arquivo,
                        "subpasta": foto.subpasta, "status": status}

                if auditor is not None and foto.caminho:
                    try:
                        b64, media = core.preparar_imagem(foto.caminho)
                        texto = auditor.avaliar(foto, b64, media)
                        foto.veredito, foto.motivo = core._parse_veredito(texto)
                    except Exception as e:  # noqa: BLE001
                        foto.veredito, foto.motivo = "INCONCLUSIVO", f"erro na IA: {e}"
                    item["veredito"] = foto.veredito
                    item["motivo"] = foto.motivo

                yield evento(item)

                if auditor is not None and getattr(auditor, "pausa", 0) and i < total:
                    time.sleep(auditor.pausa)

            core.gerar_csv(fotos, pasta_temp)
            resumo = {
                "total": total,
                "baixadas": sum(1 for f in fotos if f.caminho and f.caminho.exists()),
                "compativel": sum(1 for f in fotos if f.veredito == "COMPATIVEL"),
                "divergente": sum(1 for f in fotos if f.veredito == "DIVERGENTE"),
                "inconclusivo": sum(1 for f in fotos if f.veredito == "INCONCLUSIVO"),
            }
            zid = _zipar_diretorio(pasta_temp, info["nome_arquivo"])
            pasta_temp = None  # já foi removida por _zipar_diretorio
            yield evento({"tipo": "fim", "zip_id": zid, "resumo": resumo})
        except core.AppError as e:
            yield evento({"tipo": "erro", "msg": str(e)})
        except Exception as e:  # noqa: BLE001
            yield evento({"tipo": "erro", "msg": f"Erro inesperado: {e}"})
        finally:
            if pasta_temp is not None:
                shutil.rmtree(pasta_temp, ignore_errors=True)

    return Response(gerar(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
```

(Note: o parâmetro `forcar` some — não faz mais sentido com diretório temporário sempre vazio a cada requisição.)

- [ ] **Step 9: Trocar as rotas `/api/abrir-pasta` e `/api/csv` pela rota de zip**

Remover as duas rotas inteiras (`api_abrir_pasta` e `api_csv`). No lugar delas, adicionar:

```python
@app.route("/api/zip/<zid>")
def api_zip(zid):
    info = _zips.get(zid)
    if not info or not Path(info["caminho"]).exists():
        return "Arquivo não encontrado (pode ter expirado).", 404
    return send_file(str(info["caminho"]), as_attachment=True,
                     download_name=info["nome"])
```

- [ ] **Step 10: Rodar os testes e confirmar que passam**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS em todos os testes (agora 10).

- [ ] **Step 11: Atualizar o front-end (`templates/index.html`)**

Remover a linha do checkbox de "rebaixar":
```html
<label class="checkline"><input type="checkbox" id="forcar"> Rebaixar mesmo se já existir</label>
```
(mantendo só o checkbox "Auditar fotos com IA" na mesma `.row`).

Remover a linha:
```html
<div class="destino" id="destino"></div>
```

Trocar os botões de resultado:
```html
<button class="btn btn-primary" id="btnPasta">📂 Abrir pasta</button>
<button class="btn btn-ghost" id="btnCsv">⬇ Baixar CSV</button>
```
por:
```html
<button class="btn btn-primary" id="btnZip">⬇ Baixar .zip (fotos + auditoria.csv)</button>
```

No `<script>`, trocar `let ESTADO = { fotos:[], destino:"", csv:"", numero:null };` por:
```js
let ESTADO = { fotos:[], zipId:"", numero:null };
```

Em `renderReport(d)`, remover a linha:
```js
$("#destino").innerHTML = `Serão salvas em: <code>${esc(d.destino)}</code>`;
```

No payload de `$("#btnProcessar").onclick`, remover `forcar:$("#forcar").checked,` do objeto `payload`.

Em `handleEvent`, no `else if(ev.tipo==="inicio")`, trocar:
```js
else if(ev.tipo==="inicio"){ ESTADO.destino=ev.destino;
    $("#progTxt").textContent=`0 / ${ev.total} fotos`; logln(`Baixando para: ${ev.destino}`); }
```
por:
```js
else if(ev.tipo==="inicio"){
    $("#progTxt").textContent=`0 / ${ev.total} fotos`; logln("Baixando fotos..."); }
```

No `else if(ev.tipo==="fim")`, trocar:
```js
else if(ev.tipo==="fim"){ ESTADO.csv=ev.csv; ESTADO.destino=ev.destino;
```
por:
```js
else if(ev.tipo==="fim"){ ESTADO.zipId=ev.zip_id;
```

E trocar os handlers dos botões:
```js
$("#btnPasta").onclick=()=>fetch("/api/abrir-pasta",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({caminho:ESTADO.destino})}).then(r=>r.json()).then(d=>{ if(d.erro) toast(d.erro); });
$("#btnCsv").onclick=()=>{ if(ESTADO.csv) location.href="/api/csv?caminho="+encodeURIComponent(ESTADO.csv); };
```
por:
```js
$("#btnZip").onclick=()=>{ if(ESTADO.zipId) location.href="/api/zip/"+ESTADO.zipId; };
```

- [ ] **Step 12: Verificação manual local com um relatório real**

Rodar `python app.py`, carregar um relatório real (token válido), clicar em "Baixar fotos", esperar concluir, clicar em "Baixar .zip", confirmar que o `.zip` baixado contém as fotos nomeadas por código e o `auditoria.csv`.

- [ ] **Step 13: Commit**

```bash
git add app.py templates/index.html tests/test_app.py
git commit -m "feat: entrega dos arquivos por .zip via navegador, remove pasta Downloads/abrir-pasta"
```

---

### Task 4: Artefatos de deploy (Render) + docstring/README

**Files:**
- Modify: `app.py` (docstring do módulo, bloco `if __name__ == "__main__":`)
- Create: `Procfile`
- Modify: `requirements.txt` (adiciona `gunicorn`)
- Modify: `README.md` (seção "Deploy", corrige a seção "Segurança")

**Interfaces:**
- Consumes: `import os` (já adicionado na Task 1).
- Produces: nada que outras tasks dependam — última task do plano.

- [ ] **Step 1: Atualizar a docstring do módulo em `app.py`**

Trocar:

```python
"""
Interface web local para o Auditor de Relatórios - Diário de Obra.

Um pequeno servidor Flask que reaproveita a lógica de `auditar_relatorio.py`
e serve uma interface no navegador. Roda 100% na sua máquina: o token não
sai do seu computador e as fotos são salvas na sua pasta Downloads.

Uso:
    python app.py
    (o navegador abre sozinho em http://127.0.0.1:5000)
"""
```

por:

```python
"""
Interface web para o Auditor de Relatórios - Diário de Obra.

Um servidor Flask que reaproveita a lógica de `auditar_relatorio.py` e serve
uma interface no navegador. Cada pessoa cola seu próprio token (fica salvo
só no navegador dela, nunca no servidor); ao final do processamento, as
fotos + o auditoria.csv são baixados como um .zip.

Uso local:
    python app.py
    (o navegador abre sozinho em http://127.0.0.1:5000, sem login)

Uso hospedado (ex.: Render): rodar via `gunicorn app:app --workers 1`, com
as variáveis de ambiente APP_PASSWORD e SECRET_KEY definidas no host — ver
a seção "Deploy" do README.
"""
```

- [ ] **Step 2: Ajustar o `if __name__ == "__main__":` para funcionar em local e hospedado**

Trocar:

```python
if __name__ == "__main__":
    porta = 5000
    url = f"http://127.0.0.1:{porta}"
    print(f"\n  Auditor RDO rodando em {url}")
    print("  (feche esta janela para encerrar o aplicativo)\n")
    _abrir_navegador(url)
    # threaded=True: atende várias requisições ao mesmo tempo (miniaturas +
    # coleta longa da obra), evitando "Failed to fetch" quando uma demora.
    app.run(host="127.0.0.1", port=porta, debug=False, threaded=True)
```

por:

```python
if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    local = "PORT" not in os.environ
    host = "127.0.0.1" if local else "0.0.0.0"
    if local:
        url = f"http://127.0.0.1:{porta}"
        print(f"\n  Auditor RDO rodando em {url}")
        print("  (feche esta janela para encerrar o aplicativo)\n")
        _abrir_navegador(url)
    # threaded=True: atende várias requisições ao mesmo tempo (miniaturas +
    # coleta longa da obra), evitando "Failed to fetch" quando uma demora.
    app.run(host=host, port=porta, debug=False, threaded=True)
```

- [ ] **Step 3: Rodar a suíte inteira de novo (garantir que nada quebrou)**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS em todos os testes.

- [ ] **Step 4: Criar o `Procfile`**

```
web: gunicorn app:app --workers 1 --threads 4
```

- [ ] **Step 5: Adicionar `gunicorn` ao `requirements.txt`**

Adicionar ao final:

```
# Só necessário para hospedar (Render/produção) - o app local usa o servidor
# de desenvolvimento do Flask via `python app.py` e não precisa disso.
gunicorn>=22.0.0
```

- [ ] **Step 6: Adicionar a seção "Deploy" ao `README.md` e corrigir a seção "Segurança"**

No `README.md`, trocar a seção inteira (do título `## Segurança` até o
fim do último bullet, logo antes do fim do arquivo):

```markdown
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
```

por:

```markdown
## Segurança

Rodando localmente (`python app.py`, sem `APP_PASSWORD` definida), o app é
100% pessoal: escuta só em `127.0.0.1`, sem login, sem exposição de rede.
Boa parte de um checklist de segurança genérico para aplicações web não se
aplica a esse modo — sem banco de dados (não há RLS, criptografia de dados
em banco, mass assignment ou queries SQL para parametrizar) e sem contas
individuais por pessoa (não há senha por usuário para fazer hash).

Rodando hospedado (Render, com `APP_PASSWORD` definida — ver "Deploy"
abaixo), uma tela de login protege o app, com bloqueio por tentativas
erradas repetidas (rate limit simples). Em ambos os casos:

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
```

E adicionar, depois da seção "Segurança", uma nova seção:

```markdown
## Deploy (Render.com)

1. Suba este repositório no GitHub (se ainda não estiver).
2. Em [render.com](https://render.com), crie um **Web Service** novo,
   conectado a este repositório.
3. Configuração do serviço:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** deixe em branco (o Render lê o `Procfile`
     automaticamente) ou use `gunicorn app:app --workers 1 --threads 4`.
4. Em **Environment**, adicione as variáveis:
   - `APP_PASSWORD` — a senha que a equipe vai usar para entrar no app.
   - `SECRET_KEY` — qualquer string longa e aleatória (ex.: gerada com
     `python -c "import secrets; print(secrets.token_hex(32))"`).
5. Deploy. O Render expõe uma URL `https://` própria — HTTPS já vem pronto.
6. Compartilhe a URL e a senha com a equipe. Cada pessoa cola seu próprio
   token do Diário de Obra (e sua própria chave de IA, se for usar
   auditoria) — nada disso fica salvo no servidor.

**Importante:** o serviço precisa rodar com um único worker
(`--workers 1`, já configurado no `Procfile`) — o cache de relatórios, o
controle de tentativas de login e os zips prontos para download vivem em
memória, e workers diferentes não veem a memória um do outro.
```

- [ ] **Step 7: Commit**

```bash
git add app.py Procfile requirements.txt README.md
git commit -m "feat: artefatos de deploy (Procfile, gunicorn) e docs de deploy no Render"
```

---

## Após a implementação

Depois que as 4 tasks estiverem commitadas, falta o passo manual final (fora do escopo de código): criar o Web Service no Render seguindo a seção "Deploy" do README, configurar `APP_PASSWORD`/`SECRET_KEY`, testar o login e um fluxo completo (carregar relatório → processar → baixar zip) na URL pública, e então compartilhar a URL + senha com a equipe.
