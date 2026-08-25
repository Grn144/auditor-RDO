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


def test_rate_limit_por_ip_via_x_forwarded_for(app_module):
    """Verifica que rate limiting utiliza corretamente X-Forwarded-For
    (via ProxyFix) para identificar IPs distintos atrás de um proxy."""
    mod = app_module(app_password="segredo123")
    cliente = mod.app.test_client()

    # IP A (1.2.3.4): tenta _LOGIN_MAX_TENTATIVAS vezes com senha errada
    for _ in range(mod._LOGIN_MAX_TENTATIVAS):
        cliente.post("/login", data={"senha": "errada"},
                    headers={"X-Forwarded-For": "1.2.3.4"})

    # IP A agora deve estar bloqueado
    resp_a_bloqueado = cliente.post("/login", data={"senha": "segredo123"},
                                   headers={"X-Forwarded-For": "1.2.3.4"})
    assert "aguarde" in resp_a_bloqueado.get_data(as_text=True).lower()

    # IP B (5.6.7.8): deve ainda ter tentativas disponíveis
    resp_b_nao_bloqueado = cliente.post("/login", data={"senha": "errada"},
                                       headers={"X-Forwarded-For": "5.6.7.8"})
    # Não deve conter "aguarde" (não está bloqueado), mas conterá "incorreta"
    assert "aguarde" not in resp_b_nao_bloqueado.get_data(as_text=True).lower()
    assert "incorreta" in resp_b_nao_bloqueado.get_data(as_text=True).lower()
