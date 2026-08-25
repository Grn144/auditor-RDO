#!/usr/bin/env python3
"""
Interface web local para o Auditor de Relatórios - Diário de Obra.

Um pequeno servidor Flask que reaproveita a lógica de `auditar_relatorio.py`
e serve uma interface no navegador. Roda 100% na sua máquina: o token não
sai do seu computador e as fotos são salvas na sua pasta Downloads.

Uso:
    python app.py
    (o navegador abre sozinho em http://127.0.0.1:5000)
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
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
    _login_tentativas[ip] = tentativas
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
    Retorna (fotos, info, destino)."""
    modo, obra_id, rel_id = core.resolver_alvo(alvo)
    client = core.DiarioObraClient(token)
    if modo == "obra":
        fotos, ctx = core.coletar_fotos_obra(client, obra_id, progresso)
        destino = Path.home() / "Downloads" / core._sanitizar_pasta(f"obra {ctx.get('nome', obra_id)}")
        info = {"modo": "obra", "titulo": ctx.get("nome", obra_id),
                "subtitulo": "Obra completa (todas as tarefas)",
                "total": len(fotos), "grupos": ctx.get("grupos", 0)}
    else:
        mapa = core.montar_mapa_tarefas(client, obra_id)
        fotos, rel = core.coletar_fotos(client, obra_id, rel_id, mapa)
        destino = _destino_de(rel)
        info = {"modo": "relatorio",
                "titulo": f"Relatório nº {rel.get('numero', '?')}",
                "subtitulo": rel.get("data", ""),
                "total": len(fotos),
                "grupos": len({f.subpasta for f in fotos})}
    return fotos, info, destino


def _coletar_cacheado(token: str, alvo: str, progresso=None):
    """Como _coletar, mas reaproveita um resultado recente do mesmo alvo."""
    chave = (alvo or "").strip()
    ent = _CACHE.get(chave)
    if ent and (time.time() - ent["ts"] < _CACHE_TTL):
        return ent["fotos"], ent["info"], ent["destino"]
    fotos, info, destino = _coletar(token, alvo, progresso)
    _CACHE[chave] = {"fotos": fotos, "info": info, "destino": destino,
                     "ts": time.time()}
    return fotos, info, destino


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


def _destino_de(rel: dict) -> Path:
    pasta = f"relatorio_{rel.get('numero', 'x')}_{core.sanitizar(str(rel.get('data', '')))}"
    return Path.home() / "Downloads" / pasta


# ----------------------------------------------------------------------------
# Validação de inputs (SSRF / path traversal)
# ----------------------------------------------------------------------------

PASTA_DOWNLOADS = (Path.home() / "Downloads").resolve()


def _caminho_seguro(bruto: str) -> Path | None:
    """Resolve `bruto` e garante que ele fica dentro de ~/Downloads.
    Retorna None se o caminho for inválido ou tentar escapar da pasta."""
    if not bruto:
        return None
    try:
        caminho = Path(bruto).resolve()
    except (OSError, ValueError):
        return None
    if caminho != PASTA_DOWNLOADS and PASTA_DOWNLOADS not in caminho.parents:
        return None
    return caminho


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
        elif request.form.get("senha") == APP_PASSWORD:
            session["autenticado"] = True
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
        fotos, info, destino = _coletar_cacheado(token, body.get("alvo", ""))
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
        "destino": str(destino),
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
    forcar = bool(body.get("forcar"))
    usar_ia = bool(body.get("ia"))
    provedor = body.get("provedor", "groq")
    modelo = body.get("modelo") or core.DEFAULT_MODELS.get(provedor)
    chave_ia = (body.get("chave_ia") or "").strip()

    def evento(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False) + "\n"

    @stream_with_context
    def gerar():
        try:
            if not token:
                raise core.AppError("Informe o token do Diário de Obra.")
            yield evento({"tipo": "log", "msg": "Lendo tarefas e localizando fotos..."})
            fotos, info, destino = _coletar_cacheado(token, alvo)
            destino.mkdir(parents=True, exist_ok=True)
            total = len(fotos)
            yield evento({"tipo": "inicio", "total": total,
                          "destino": str(destino), "com_ia": usar_ia})

            auditor = None
            if usar_ia:
                auditor = core.criar_auditor(provedor, modelo, chave_ia)

            for i, foto in enumerate(fotos, start=1):
                pasta = destino / foto.subpasta if foto.subpasta else destino
                pasta.mkdir(parents=True, exist_ok=True)
                caminho = pasta / foto.nome_arquivo
                foto.caminho = caminho
                status = "baixado"
                try:
                    if caminho.exists() and not forcar:
                        status = "existe"
                    else:
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
                    import time
                    time.sleep(auditor.pausa)

            csv_path = core.gerar_csv(fotos, destino)
            resumo = {
                "total": total,
                "baixadas": sum(1 for f in fotos if f.caminho and f.caminho.exists()),
                "compativel": sum(1 for f in fotos if f.veredito == "COMPATIVEL"),
                "divergente": sum(1 for f in fotos if f.veredito == "DIVERGENTE"),
                "inconclusivo": sum(1 for f in fotos if f.veredito == "INCONCLUSIVO"),
            }
            yield evento({"tipo": "fim", "csv": str(csv_path),
                          "destino": str(destino), "resumo": resumo})
        except core.AppError as e:
            yield evento({"tipo": "erro", "msg": str(e)})
        except Exception as e:  # noqa: BLE001
            yield evento({"tipo": "erro", "msg": f"Erro inesperado: {e}"})

    return Response(gerar(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/abrir-pasta", methods=["POST"])
def api_abrir_pasta():
    body = request.get_json(force=True)
    caminho = _caminho_seguro(body.get("caminho", ""))
    if caminho is None:
        return jsonify({"erro": "Caminho inválido."}), 400
    if not caminho.exists():
        return jsonify({"erro": "Pasta não encontrada."}), 404
    try:
        import os
        os.startfile(str(caminho))  # type: ignore[attr-defined]  # Windows
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": str(e)}), 500


@app.route("/api/csv")
def api_csv():
    caminho = _caminho_seguro(request.args.get("caminho", ""))
    if caminho is None or not caminho.exists():
        return "CSV não encontrado", 404
    return send_file(str(caminho), as_attachment=True,
                     download_name=caminho.name)


def _abrir_navegador(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    porta = 5000
    url = f"http://127.0.0.1:{porta}"
    print(f"\n  Auditor RDO rodando em {url}")
    print("  (feche esta janela para encerrar o aplicativo)\n")
    _abrir_navegador(url)
    # threaded=True: atende várias requisições ao mesmo tempo (miniaturas +
    # coleta longa da obra), evitando "Failed to fetch" quando uma demora.
    app.run(host="127.0.0.1", port=porta, debug=False, threaded=True)
