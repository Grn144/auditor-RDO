#!/usr/bin/env python3
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
as variáveis de ambiente APP_USERS (usuários e senhas com hash) e SECRET_KEY
definidas no host — ver a seção "Deploy" do README.
"""

from __future__ import annotations

import copy
import io
import ipaddress
import json
import os
import queue
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, request,
                   render_template, send_file, session,
                   stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import auditar_relatorio as core
import relatorio_pdf

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

try:
    APP_USERS: dict[str, str] = json.loads(os.environ.get("APP_USERS") or "{}")
except ValueError:
    APP_USERS = {}
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(APP_USERS),
)

_LOGIN_JANELA = 15 * 60  # 15 minutos
_LOGIN_MAX_TENTATIVAS = 10
_login_tentativas: dict[str, list[float]] = {}

_SESSAO_TIMEOUT = 30 * 60  # 30 minutos de inatividade


def _registrar_tentativa_falha(ip: str) -> None:
    agora = time.time()
    tentativas = [t for t in _login_tentativas.get(ip, []) if agora - t < _LOGIN_JANELA]
    tentativas.append(agora)
    _login_tentativas[ip] = tentativas


def _ip_bloqueado(ip: str) -> bool:
    agora = time.time()
    tentativas = [t for t in _login_tentativas.get(ip, []) if agora - t < _LOGIN_JANELA]
    if tentativas:
        _login_tentativas[ip] = tentativas
    else:
        _login_tentativas.pop(ip, None)
    return len(tentativas) >= _LOGIN_MAX_TENTATIVAS

# Cache curto do resultado da coleta, para o "baixar" não refazer o trabalho
# que o "carregar" já fez (importante em obras grandes, que levam dezenas de s).
_CACHE: dict = {}
_CACHE_TTL = 600  # 10 minutos


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

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
                "nome_arquivo": core._sanitizar_pasta(f"obra {ctx.get('nome', obra_id)}"),
                "cabecalho": ctx.get("cabecalho") or {},
                "atividades": ctx.get("atividades") or []}
    else:
        mapa, _atividades = core.montar_mapa_tarefas(client, obra_id)
        fotos, rel = core.coletar_fotos(client, obra_id, rel_id, mapa)
        info = {"modo": "relatorio",
                "titulo": f"Relatório nº {rel.get('numero', '?')}",
                "subtitulo": rel.get("data", ""),
                "total": len(fotos),
                "grupos": len({f.subpasta for f in fotos}),
                "nome_arquivo": f"relatorio_{rel.get('numero', 'x')}_{core.sanitizar(str(rel.get('data', '')))}"}
    return fotos, info


def _coletar_cacheado(token: str, alvo: str, progresso=None):
    """Como _coletar, mas reaproveita um resultado recente do mesmo alvo.

    Cada chamador recebe sua PRÓPRIA cópia da lista de `Foto` (deep-copy),
    nunca os objetos originais guardados em `_CACHE`: `Foto` é um dataclass
    mutável e `api_processar` escreve diretamente em `foto.caminho`,
    `foto.veredito`, `foto.motivo` — sem a cópia, duas pessoas (ou duas
    requisições concorrentes, com `--threads 4`) processando o mesmo
    relatório dentro do TTL do cache compartilhariam e sobrescreveriam os
    vereditos uma da outra."""
    chave = (alvo or "").strip()
    ent = _CACHE.get(chave)
    if ent and (time.time() - ent["ts"] < _CACHE_TTL):
        return copy.deepcopy(ent["fotos"]), ent["info"]
    fotos, info = _coletar(token, alvo, progresso)
    _CACHE[chave] = {"fotos": copy.deepcopy(fotos), "info": info, "ts": time.time()}
    return fotos, info


def _foto_dict(f: core.Foto) -> dict:
    return {
        "uid": f"{f.subpasta}/{f.nome_arquivo}",
        "codigo": f.codigo,
        "grupo": f.grupo,
        "grupo_desc": f.grupo_desc,
        "subpasta": f.subpasta,
        "nome_arquivo": f.nome_arquivo,
        "descricao": f.descricao,
        "url": f.url,
        "miniatura": f.miniatura or f.url,
    }


# ----------------------------------------------------------------------------
# Validação de inputs (SSRF na imagem proxied)
# ----------------------------------------------------------------------------

_DNS_CACHE_TTL = 300  # 5 minutos
_dns_cache: dict[str, tuple[bool, float]] = {}  # host -> (é_seguro, quando)


def _host_e_seguro(host: str) -> bool:
    """Resolve `host` e diz se todos os IPs são públicos (não privados/
    loopback/link-local/reservados/multicast). Guarda o resultado em cache
    por `_DNS_CACHE_TTL` — sem isso, um relatório com dezenas de fotos do
    mesmo domínio de CDN repete a mesma busca de DNS a cada foto."""
    entrada = _dns_cache.get(host)
    if entrada is not None and time.time() - entrada[1] < _DNS_CACHE_TTL:
        return entrada[0]
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        seguro = False
    else:
        seguro = all(
            not (ip := ipaddress.ip_address(info[4][0])).is_private
            and not ip.is_loopback and not ip.is_link_local
            and not ip.is_reserved and not ip.is_multicast
            for info in infos
        )
    _dns_cache[host] = (seguro, time.time())
    return seguro


def _url_de_imagem_segura(u: str) -> bool:
    """Só permite proxy de imagens https, bloqueando localhost/IPs privados
    (evita que o proxy seja usado para acessar rede interna - SSRF)."""
    try:
        partes = urllib.parse.urlparse(u)
    except ValueError:
        return False
    if partes.scheme != "https" or not partes.hostname:
        return False
    return _host_e_seguro(partes.hostname)


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


# ----------------------------------------------------------------------------
# Rotas
# ----------------------------------------------------------------------------

@app.after_request
def _adicionar_cabecalhos_seguranca(resp: Response) -> Response:
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


@app.before_request
def _exigir_login():
    if not APP_USERS:
        return None
    if request.endpoint in ("login", "static"):
        return None
    ultimo_acesso = session.get("ultimo_acesso")
    if session.get("autenticado") and ultimo_acesso is not None \
            and time.time() - ultimo_acesso < _SESSAO_TIMEOUT:
        session["ultimo_acesso"] = time.time()  # renova (sliding window)
        return None
    session.clear()
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        ip = request.remote_addr or "desconhecido"
        usuario = (request.form.get("usuario") or "").strip()
        senha = request.form.get("senha", "")
        hash_salvo = APP_USERS.get(usuario)
        if _ip_bloqueado(ip):
            erro = "Muitas tentativas erradas. Aguarde alguns minutos."
        elif hash_salvo and check_password_hash(hash_salvo, senha):
            session["autenticado"] = True
            session["usuario"] = usuario
            session["ultimo_acesso"] = time.time()
            _login_tentativas.pop(ip, None)
            print(f"[login] {usuario!r} autenticou (ip={ip})", flush=True)
            return redirect(url_for("index"))
        else:
            _registrar_tentativa_falha(ip)
            erro = "Usuário ou senha incorretos."
    return render_template("login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("index.html", usuario=session.get("usuario"))


@app.route("/api/carregar", methods=["POST"])
def api_carregar():
    body = request.get_json(force=True)
    token = (body.get("token") or "").strip()
    alvo = body.get("alvo", "")

    def evento(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False) + "\n"

    @stream_with_context
    def gerar():
        if not token:
            yield evento({"tipo": "erro", "msg": "Informe o token do Diário de Obra."})
            return

        # A coleta roda numa thread à parte, mandando o progresso por uma
        # fila — assim dá pra ir transmitindo os eventos conforme chegam
        # (ex.: "12/54 tarefas...") em vez de só no final, útil em obras
        # grandes onde a coleta de todas as tarefas demora.
        fila: queue.Queue = queue.Queue()
        resultado: dict = {}

        def progresso(msg, atual=None, total=None):
            fila.put({"tipo": "progresso", "msg": msg, "atual": atual, "total": total})

        def trabalhar():
            try:
                fotos, info = _coletar_cacheado(token, alvo, progresso)
                resultado["fotos"] = fotos
                resultado["info"] = info
            except core.AppError as e:
                resultado["erro"] = str(e)
            except Exception as e:  # noqa: BLE001
                resultado["erro"] = f"Erro inesperado: {e}"
            finally:
                fila.put(None)  # sentinela: coleta terminou

        threading.Thread(target=trabalhar, daemon=True).start()

        while True:
            item = fila.get()
            if item is None:
                break
            yield evento(item)

        if "erro" in resultado:
            yield evento({"tipo": "erro", "msg": resultado["erro"]})
            return

        fotos, info = resultado["fotos"], resultado["info"]
        yield evento({
            "tipo": "fim",
            "modo": info["modo"],
            "titulo": info["titulo"],
            "subtitulo": info["subtitulo"],
            "total": info["total"],
            "grupos": info["grupos"],
            "tarefas": len({f.codigo for f in fotos}),
            "fotos": [_foto_dict(f) for f in fotos],
        })

    return Response(gerar(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/img")
def api_img():
    """Proxy das imagens (evita bloqueio de hotlink/CORS na galeria)."""
    u = request.args.get("u", "")
    if not _url_de_imagem_segura(u):
        return "url inválida", 400
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "auditor-rdo/1.0"})
        resp = urllib.request.urlopen(req, timeout=60, context=core.SSL_CONTEXT)
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        return Response(resp.read(), mimetype=ctype,
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:  # noqa: BLE001
        return f"erro ao carregar imagem: {e}", 502


@app.route("/api/processar", methods=["POST"])
def api_processar():
    body = request.get_json(force=True)
    token = (body.get("token") or "").strip()
    alvo = body.get("alvo", "")
    usar_ia = bool(body.get("ia"))
    provedor = body.get("provedor", "groq")
    modelo = body.get("modelo") or core.DEFAULT_MODELS.get(provedor)
    chave_ia = (body.get("chave_ia") or "").strip()
    print(f"[processar] usuario={session.get('usuario')!r} alvo={alvo!r} ia={usar_ia}",
          flush=True)

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
            fotos_meta = [
                {"codigo": f.codigo, "descricao": core._descricao_curta(f.descricao),
                 "subpasta": f.subpasta, "grupo_desc": f.grupo_desc,
                 "nome_arquivo": f.nome_arquivo}
                for f in fotos if f.caminho and f.caminho.exists()
            ]
            zid = _zipar_diretorio(pasta_temp, info["nome_arquivo"])
            pasta_temp = None  # já foi removida por _zipar_diretorio
            _zips[zid]["info"] = info
            _zips[zid]["fotos_meta"] = fotos_meta
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


@app.route("/api/zip/<zid>")
def api_zip(zid):
    info = _zips.get(zid)
    if not info or not Path(info["caminho"]).exists():
        return "Arquivo não encontrado (pode ter expirado).", 404
    return send_file(str(info["caminho"]), as_attachment=True,
                     download_name=info["nome"])


def _montar_grupos_fotos(zf: zipfile.ZipFile, fotos_meta: list[dict]) -> list[dict]:
    """Reagrupa as fotos já baixadas (guardadas no zip) por grupo/tarefa.

    Não lê nenhum byte de imagem aqui — cada entrada de foto vira uma
    FUNÇÃO que lê do zip só quando chamada. `zf` precisa continuar aberto
    enquanto o PDF é montado (é o chamador que garante isso). Ler tudo de
    uma vez numa lista, como fazia antes, estourava a memória do Render
    free tier (512MB) em relatórios com centenas de fotos."""
    grupos: dict[str, dict] = {}
    ordem: list[str] = []
    for m in fotos_meta:
        chave = m["subpasta"] or "sem_grupo"
        if chave not in grupos:
            grupos[chave] = {"letra": chave, "desc": m.get("grupo_desc", ""),
                             "tarefas": {}}
            ordem.append(chave)
        tarefas = grupos[chave]["tarefas"]
        cod = m["codigo"]
        if cod not in tarefas:
            tarefas[cod] = {"codigo": f"{chave.upper()}{cod}",
                            "descricao": m["descricao"], "fotos": []}
        caminho_interno = (f"{m['subpasta']}/{m['nome_arquivo']}"
                           if m["subpasta"] else m["nome_arquivo"])

        def carregar(caminho=caminho_interno):
            try:
                return zf.read(caminho)
            except KeyError:
                return None

        tarefas[cod]["fotos"].append(carregar)
    return [{"letra": grupos[k]["letra"], "desc": grupos[k]["desc"],
            "tarefas": list(grupos[k]["tarefas"].values())} for k in ordem]


def _resumo_pdf(atividades: list[dict], fotos_meta: list[dict]) -> dict:
    """Números da faixa de resumo do cabeçalho do PDF.

    "tarefas" e "grupos" usam o total REAL da obra (`atividades` — a
    mesma lista completa que vira a tabela de ATIVIDADES, com ou sem
    foto), não só o que foi fotografado — senão o resumo mostra um
    número menor que a própria tabela logo abaixo dele. Só cai pra
    contar via `fotos_meta` se `atividades` vier vazio (ex.: falha ao
    buscar o cabeçalho da obra), pra não mostrar "0 tarefas" tendo foto."""
    return {
        "tarefas": len(atividades) or len({(m["subpasta"], m["codigo"]) for m in fotos_meta}),
        "fotos": len(fotos_meta),
        "grupos": len({a["grupo"] for a in atividades}) or len({m["subpasta"] for m in fotos_meta}),
    }


@app.route("/api/pdf/<zid>")
def api_pdf(zid):
    entry = _zips.get(zid)
    if not entry or not Path(entry["caminho"]).exists():
        return "Arquivo não encontrado (pode ter expirado).", 404

    info = entry.get("info") or {}
    if info.get("modo") != "obra":
        return ("Relatório em PDF disponível só para o modo \"obra completa\" "
                "por enquanto.", 400)

    cabecalho = info.get("cabecalho") or {}
    atividades = info.get("atividades") or []
    fotos_meta = entry.get("fotos_meta") or []

    imagem_obra = None
    foto_url = cabecalho.get("fotoUrl")
    if foto_url and _url_de_imagem_segura(foto_url):
        try:
            req = urllib.request.Request(foto_url, headers={"User-Agent": "auditor-rdo/1.0"})
            with urllib.request.urlopen(req, timeout=15, context=core.SSL_CONTEXT) as resp:
                imagem_obra = resp.read()
        except Exception:  # noqa: BLE001 — imagem da obra é só um extra; segue sem ela
            imagem_obra = None

    try:
        resumo = _resumo_pdf(atividades, fotos_meta)
        gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")
        # O zip precisa continuar aberto durante toda a montagem do PDF:
        # cada foto só é lida dele no momento em que o card é desenhado
        # (ver _montar_grupos_fotos), não antes.
        with zipfile.ZipFile(entry["caminho"]) as zf:
            grupos_fotos = _montar_grupos_fotos(zf, fotos_meta)
            pdf_bytes = relatorio_pdf.gerar_pdf(cabecalho, atividades, resumo,
                                                grupos_fotos, gerado_em,
                                                imagem_obra=imagem_obra)
    except Exception as e:  # noqa: BLE001
        return f"Erro ao gerar o PDF: {e}", 500

    nome_pdf = info.get("nome_arquivo", entry["nome"].rsplit(".", 1)[0]) + ".pdf"
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name=nome_pdf, mimetype="application/pdf")


def _abrir_navegador(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


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
