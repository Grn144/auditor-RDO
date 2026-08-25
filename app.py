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
as variáveis de ambiente APP_PASSWORD e SECRET_KEY definidas no host — ver
a seção "Deploy" do README.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
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
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, request,
                   render_template, send_file, session,
                   stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import auditar_relatorio as core

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

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

def _url_de_imagem_segura(u: str) -> bool:
    """Só permite proxy de imagens https, bloqueando localhost/IPs privados
    (evita que o proxy seja usado para acessar rede interna - SSRF)."""
    try:
        partes = urllib.parse.urlparse(u)
    except ValueError:
        return False
    if partes.scheme != "https" or not partes.hostname:
        return False
    try:
        infos = socket.getaddrinfo(partes.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


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
        elif secrets.compare_digest(request.form.get("senha", ""), APP_PASSWORD or ""):
            session["autenticado"] = True
            _login_tentativas.pop(ip, None)
            return redirect(url_for("index"))
        else:
            _registrar_tentativa_falha(ip)
            erro = "Senha incorreta."
    return render_template("login.html", erro=erro)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/carregar", methods=["POST"])
def api_carregar():
    body = request.get_json(force=True)
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"erro": "Informe o token do Diário de Obra."}), 400
    try:
        fotos, info = _coletar_cacheado(token, body.get("alvo", ""))
    except core.AppError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": f"Erro inesperado: {e}"}), 500

    return jsonify({
        "modo": info["modo"],
        "titulo": info["titulo"],
        "subtitulo": info["subtitulo"],
        "total": info["total"],
        "grupos": info["grupos"],
        "tarefas": len({f.codigo for f in fotos}),
        "fotos": [_foto_dict(f) for f in fotos],
    })


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


@app.route("/api/zip/<zid>")
def api_zip(zid):
    info = _zips.get(zid)
    if not info or not Path(info["caminho"]).exists():
        return "Arquivo não encontrado (pode ter expirado).", 404
    return send_file(str(info["caminho"]), as_attachment=True,
                     download_name=info["nome"])


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
