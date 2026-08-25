import importlib
import json
import time

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def app_module(monkeypatch):
    """Recarrega app.py com env vars controladas para cada teste."""
    import app as _app_module

    def _carregar(usuarios=None, secret_key="teste-secret"):
        if usuarios is None:
            monkeypatch.delenv("APP_USERS", raising=False)
        else:
            # `usuarios` chega em texto puro (mais legível no teste); o
            # fixture converte pra hash antes de simular a env var, do
            # mesmo jeito que o valor real configurado no Render deve ser.
            hashes = {nome: generate_password_hash(senha)
                      for nome, senha in usuarios.items()}
            monkeypatch.setenv("APP_USERS", json.dumps(hashes))
        monkeypatch.setenv("SECRET_KEY", secret_key)
        importlib.reload(_app_module)
        _app_module.app.testing = True
        return _app_module

    return _carregar


def test_sem_usuarios_configurados_nao_exige_login(app_module):
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/")
    assert resp.status_code == 200


def test_com_usuarios_configurados_redireciona_para_login(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_com_usuario_e_senha_certos_libera_acesso(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"},
                        follow_redirects=True)
    assert resp.status_code == 200
    resp2 = cliente.get("/")
    assert resp2.status_code == 200


def test_senha_e_guardada_como_hash_nunca_em_texto_puro(app_module):
    """A variável de ambiente (o que fica salvo/visível no Render) nunca
    pode conter a senha em texto puro — só o hash."""
    mod = app_module(usuarios={"joao": "segredo123"})
    valor_env = mod.os.environ["APP_USERS"]
    assert "segredo123" not in valor_env
    assert mod.APP_USERS["joao"] != "segredo123"


def test_login_com_usuario_inexistente_nao_libera(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"usuario": "carlos", "senha": "segredo123"})
    assert "incorretos" in resp.get_data(as_text=True).lower()
    resp2 = cliente.get("/", follow_redirects=False)
    assert resp2.status_code == 302


def test_login_com_senha_errada_nao_libera(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"usuario": "joao", "senha": "errada"})
    assert "incorretos" in resp.get_data(as_text=True).lower()
    resp2 = cliente.get("/", follow_redirects=False)
    assert resp2.status_code == 302


def test_sessao_guarda_qual_usuario_logou(app_module):
    """Motivo de existir usuário nomeado em vez de senha única:
    accountability — a sessão precisa saber quem é."""
    mod = app_module(usuarios={"joao": "segredo123", "maria": "outrasenha"})
    cliente = mod.app.test_client()
    cliente.post("/login", data={"usuario": "maria", "senha": "outrasenha"})
    with cliente.session_transaction() as sess:
        assert sess["usuario"] == "maria"


def test_cookie_de_sessao_morre_ao_fechar_o_navegador(app_module):
    """O cookie não pode ter Max-Age/Expires: precisa ser um cookie de
    sessão de verdade, que o navegador descarta ao fechar (não só a aba,
    o navegador inteiro) — a expiração por inatividade é controlada à
    parte, via timestamp guardado na própria sessão."""
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})
    cookie_sessao = next(c for c in resp.headers.getlist("Set-Cookie")
                          if c.startswith("session="))
    assert "Max-Age" not in cookie_sessao
    assert "Expires" not in cookie_sessao


def test_sessao_expira_apos_30_minutos_de_inatividade(app_module, monkeypatch):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})
    assert cliente.get("/").status_code == 200  # autenticado logo após o login

    agora = time.time()
    monkeypatch.setattr(mod.time, "time", lambda: agora + mod._SESSAO_TIMEOUT + 1)
    resp = cliente.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_atividade_renova_a_sessao_antes_do_timeout(app_module, monkeypatch):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})

    agora = time.time()
    # 20 min depois (dentro dos 30 min) — deve continuar autenticado e
    # renovar o timestamp.
    monkeypatch.setattr(mod.time, "time", lambda: agora + 20 * 60)
    assert cliente.get("/").status_code == 200

    # Mais 20 min a partir da última atividade (não do login original) —
    # ainda dentro dos 30 min de inatividade porque a atividade renovou.
    monkeypatch.setattr(mod.time, "time", lambda: agora + 40 * 60)
    assert cliente.get("/").status_code == 200


def test_bloqueia_apos_muitas_tentativas_erradas(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    for _ in range(mod._LOGIN_MAX_TENTATIVAS):
        cliente.post("/login", data={"usuario": "joao", "senha": "errada"})
    resp = cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})
    assert "aguarde" in resp.get_data(as_text=True).lower()


def test_rate_limit_por_ip_via_x_forwarded_for(app_module):
    """Verifica que rate limiting utiliza corretamente X-Forwarded-For
    (via ProxyFix) para identificar IPs distintos atrás de um proxy."""
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()

    # IP A (1.2.3.4): tenta _LOGIN_MAX_TENTATIVAS vezes com senha errada
    for _ in range(mod._LOGIN_MAX_TENTATIVAS):
        cliente.post("/login", data={"usuario": "joao", "senha": "errada"},
                    headers={"X-Forwarded-For": "1.2.3.4"})

    # IP A agora deve estar bloqueado
    resp_a_bloqueado = cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"},
                                   headers={"X-Forwarded-For": "1.2.3.4"})
    assert "aguarde" in resp_a_bloqueado.get_data(as_text=True).lower()

    # IP B (5.6.7.8): deve ainda ter tentativas disponíveis
    resp_b_nao_bloqueado = cliente.post("/login", data={"usuario": "joao", "senha": "errada"},
                                       headers={"X-Forwarded-For": "5.6.7.8"})
    # Não deve conter "aguarde" (não está bloqueado), mas conterá "incorretos"
    assert "aguarde" not in resp_b_nao_bloqueado.get_data(as_text=True).lower()
    assert "incorretos" in resp_b_nao_bloqueado.get_data(as_text=True).lower()


def test_rotas_de_config_nao_existem_mais(app_module):
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    assert cliente.get("/api/config").status_code == 404
    assert cliente.post("/api/config", json={}).status_code == 404
    assert cliente.post("/api/config/limpar").status_code == 404


def test_zip_helpers_e_rota_download(app_module, tmp_path):
    mod = app_module(usuarios=None)
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
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/api/zip/nao-existe")
    assert resp.status_code == 404


def test_limpar_zips_antigos_remove_expirados(app_module, tmp_path):
    mod = app_module(usuarios=None)
    arquivo_zip = tmp_path / "velho.zip"
    arquivo_zip.write_bytes(b"conteudo")
    zid = "velho"
    mod._zips[zid] = {"caminho": arquivo_zip, "nome": "velho.zip",
                       "criado_em": time.time() - mod._ZIP_TTL - 10}
    mod._limpar_zips_antigos()
    assert zid not in mod._zips
    assert not arquivo_zip.exists()


def test_zip_exige_login_quando_usuarios_configurados(app_module):
    """Pina que o before_request cobre a rota /api/zip/<id> (adicionada na
    Task 3): mesmo um id inexistente deve ser barrado pelo login ANTES de a
    lógica da rota rodar, e não vazar um 404 "sem senha" para quem não
    autenticou."""
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.get("/api/zip/nao-existe-e-nao-importa", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_rotas_antigas_de_arquivo_nao_existem_mais(app_module):
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    assert cliente.post("/api/abrir-pasta", json={}).status_code == 404
    assert cliente.get("/api/csv").status_code == 404
