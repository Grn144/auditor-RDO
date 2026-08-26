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


def test_pdf_rota_gera_relatorio_valido(app_module, tmp_path):
    mod = app_module(usuarios=None)
    pasta = tmp_path / "fotos_teste"
    (pasta / "a").mkdir(parents=True)
    (pasta / "a" / "A1.jpg").write_bytes(b"fake-jpg-bytes")

    zid = mod._zipar_diretorio(pasta, "obra_teste")
    mod._zips[zid]["info"] = {
        "modo": "obra",
        "cabecalho": {"nome": "OBRA TESTE", "cliente": "ACME"},
        "atividades": [{"grupo": "A", "grupo_desc": "PRÉ-OBRA", "codigo": "1",
                        "descricao": "TAREFA 1", "porcentagem": 100,
                        "quantidade": 1, "unidade": "VB"}],
        "nome_arquivo": "obra_teste",
    }
    mod._zips[zid]["fotos_meta"] = [
        {"codigo": "1", "descricao": "TAREFA 1", "subpasta": "a",
         "grupo_desc": "PRÉ-OBRA", "nome_arquivo": "A1.jpg"},
    ]

    cliente = mod.app.test_client()
    resp = cliente.get(f"/api/pdf/{zid}")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"
    assert resp.headers["Content-Type"] == "application/pdf"
    assert "obra_teste.pdf" in resp.headers.get("Content-Disposition", "")


def test_resumo_pdf_conta_total_de_tarefas_nao_so_as_fotografadas(app_module):
    """Bug reportado: numa obra com 314 tarefas no total mas só algumas
    com foto, o resumo do PDF mostrava só a contagem das fotografadas —
    um número menor que a própria tabela de ATIVIDADES logo abaixo, que
    lista as 314. "tarefas"/"grupos" tem que refletir o total real da
    obra (vem de `atividades`, não de `fotos_meta`)."""
    mod = app_module(usuarios=None)
    atividades = (
        [{"grupo": "A", "grupo_desc": "PRÉ-OBRA", "codigo": str(i),
          "descricao": f"TAREFA {i}", "porcentagem": 100,
          "quantidade": 1, "unidade": "VB"} for i in range(1, 301)]
        + [{"grupo": "B", "grupo_desc": "DADOS", "codigo": str(i),
            "descricao": f"TAREFA B{i}", "porcentagem": 0,
            "quantidade": None, "unidade": None} for i in range(1, 15)]
    )
    assert len(atividades) == 314
    # só 2 dessas 314 tarefas têm foto de verdade
    fotos_meta = [
        {"codigo": "1", "descricao": "TAREFA 1", "subpasta": "a",
         "grupo_desc": "PRÉ-OBRA", "nome_arquivo": "A1.jpg"},
        {"codigo": "2", "descricao": "TAREFA 2", "subpasta": "a",
         "grupo_desc": "PRÉ-OBRA", "nome_arquivo": "A2.jpg"},
    ]

    resumo = mod._resumo_pdf(atividades, fotos_meta)

    assert resumo["tarefas"] == 314
    assert resumo["grupos"] == 2  # A e B, não só "a" (grupo das fotografadas)
    assert resumo["fotos"] == 2


def test_resumo_pdf_cai_pra_contagem_por_foto_se_atividades_vier_vazio(app_module):
    """Se por algum motivo o cabeçalho da obra não veio (`atividades`
    vazio), o resumo não pode mostrar "0 tarefas" tendo foto — cai pra
    contar pelas fotos mesmo, como fazia antes."""
    mod = app_module(usuarios=None)
    fotos_meta = [
        {"codigo": "1", "descricao": "TAREFA 1", "subpasta": "a",
         "grupo_desc": "PRÉ-OBRA", "nome_arquivo": "A1.jpg"},
    ]

    resumo = mod._resumo_pdf([], fotos_meta)

    assert resumo["tarefas"] == 1
    assert resumo["grupos"] == 1


def test_pdf_id_desconhecido_da_404(app_module):
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/api/pdf/nao-existe")
    assert resp.status_code == 404


def test_pdf_recusa_modo_relatorio(app_module, tmp_path):
    """O relatório em PDF só está disponível pro modo "obra completa" —
    modo "relatório específico" não tem cabeçalho/atividades coletados."""
    mod = app_module(usuarios=None)
    arquivo_zip = tmp_path / "rel.zip"
    arquivo_zip.write_bytes(b"conteudo")
    zid = "rel1"
    mod._zips[zid] = {"caminho": arquivo_zip, "nome": "rel.zip",
                      "criado_em": time.time(),
                      "info": {"modo": "relatorio"}}
    cliente = mod.app.test_client()
    resp = cliente.get(f"/api/pdf/{zid}")
    assert resp.status_code == 400


def test_pdf_exige_login_quando_usuarios_configurados(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    resp = cliente.get("/api/pdf/nao-existe-e-nao-importa", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


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


def _stub_getaddrinfo_publico(host, *args, **kwargs):
    """Substitui a resolução DNS real por um IP público fixo, pra testar a
    validação sem depender de rede de verdade."""
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_validacao_de_url_de_imagem_faz_cache_da_resolucao_dns(app_module, monkeypatch):
    """A busca de DNS pra validar a URL não pode se repetir a cada foto do
    mesmo domínio — isso soma muito tempo num relatório com dezenas de
    fotos. Deve resolver uma vez e reaproveitar por um tempo (TTL)."""
    mod = app_module(usuarios=None)
    chamadas = []

    def contador(host, *args, **kwargs):
        chamadas.append(host)
        return _stub_getaddrinfo_publico(host, *args, **kwargs)

    monkeypatch.setattr(mod.socket, "getaddrinfo", contador)

    assert mod._url_de_imagem_segura("https://cdn.exemplo.com/a.jpg") is True
    assert mod._url_de_imagem_segura("https://cdn.exemplo.com/b.jpg") is True
    assert mod._url_de_imagem_segura("https://cdn.exemplo.com/c.jpg") is True
    assert chamadas == ["cdn.exemplo.com"]  # só resolveu uma vez


def test_cache_de_dns_expira_apos_o_ttl(app_module, monkeypatch):
    mod = app_module(usuarios=None)
    monkeypatch.setattr(mod.socket, "getaddrinfo", _stub_getaddrinfo_publico)

    agora = time.time()
    monkeypatch.setattr(mod.time, "time", lambda: agora)
    assert mod._url_de_imagem_segura("https://cdn.exemplo.com/a.jpg") is True

    chamadas = []

    def contador(host, *args, **kwargs):
        chamadas.append(host)
        return _stub_getaddrinfo_publico(host, *args, **kwargs)

    monkeypatch.setattr(mod.socket, "getaddrinfo", contador)
    monkeypatch.setattr(mod.time, "time", lambda: agora + mod._DNS_CACHE_TTL + 1)
    assert mod._url_de_imagem_segura("https://cdn.exemplo.com/b.jpg") is True
    assert chamadas == ["cdn.exemplo.com"]  # expirou, resolveu de novo


def test_cache_de_dns_ainda_bloqueia_ip_privado(app_module, monkeypatch):
    """O cache não pode enfraquecer a proteção contra SSRF."""
    mod = app_module(usuarios=None)

    def stub_privado(host, *args, **kwargs):
        return [(2, 1, 6, "", ("192.168.1.1", 0))]

    monkeypatch.setattr(mod.socket, "getaddrinfo", stub_privado)
    assert mod._url_de_imagem_segura("https://interno.exemplo.com/a.jpg") is False


def test_carregar_transmite_eventos_de_progresso_e_termina_com_fim(app_module, monkeypatch):
    """/api/carregar precisa ser um stream (como /api/processar): cada
    chamada do callback de progresso vira um evento ndjson, terminando
    com um evento "fim" com os dados do relatório/obra — é o que dá pra
    barra de progresso "12/54 tarefas..." em obras grandes."""
    mod = app_module(usuarios=None)

    def _coletar_cacheado_fake(token, alvo, progresso=None):
        progresso("(1/2) A1: 3 fotos", 1, 2)
        progresso("(2/2) A2: 5 fotos", 2, 2)
        return [], {"modo": "obra", "titulo": "Obra Teste", "subtitulo": "sub",
                    "total": 8, "grupos": 1}

    monkeypatch.setattr(mod, "_coletar_cacheado", _coletar_cacheado_fake)

    cliente = mod.app.test_client()
    resp = cliente.post("/api/carregar", json={"token": "tok", "alvo": "algum-id"})
    linhas = [json.loads(l) for l in resp.get_data(as_text=True).splitlines() if l.strip()]

    progresso_eventos = [e for e in linhas if e["tipo"] == "progresso"]
    assert progresso_eventos == [
        {"tipo": "progresso", "msg": "(1/2) A1: 3 fotos", "atual": 1, "total": 2},
        {"tipo": "progresso", "msg": "(2/2) A2: 5 fotos", "atual": 2, "total": 2},
    ]

    evento_fim = linhas[-1]
    assert evento_fim["tipo"] == "fim"
    assert evento_fim["titulo"] == "Obra Teste"
    assert evento_fim["total"] == 8


def test_carregar_sem_token_emite_evento_de_erro(app_module):
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    resp = cliente.post("/api/carregar", json={"token": "", "alvo": "algum-id"})
    linhas = [json.loads(l) for l in resp.get_data(as_text=True).splitlines() if l.strip()]
    assert linhas[-1]["tipo"] == "erro"
    assert "token" in linhas[-1]["msg"].lower()


def test_carregar_propaga_apperror_como_evento_de_erro(app_module, monkeypatch):
    def _coletar_cacheado_fake(token, alvo, progresso=None):
        raise mod.core.AppError("relatório não encontrado")

    mod = app_module(usuarios=None)
    monkeypatch.setattr(mod, "_coletar_cacheado", _coletar_cacheado_fake)

    cliente = mod.app.test_client()
    resp = cliente.post("/api/carregar", json={"token": "tok", "alvo": "algum-id"})
    linhas = [json.loads(l) for l in resp.get_data(as_text=True).splitlines() if l.strip()]
    assert linhas[-1]["tipo"] == "erro"
    assert "não encontrado" in linhas[-1]["msg"]


def test_logout_limpa_sessao_e_exige_login_de_novo(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})
    assert cliente.get("/").status_code == 200  # autenticado

    resp = cliente.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    resp2 = cliente.get("/", follow_redirects=False)
    assert resp2.status_code == 302  # sessão foi limpa, exige login de novo
    assert "/login" in resp2.headers["Location"]


def test_pagina_principal_mostra_usuario_logado(app_module):
    mod = app_module(usuarios={"joao": "segredo123"})
    cliente = mod.app.test_client()
    cliente.post("/login", data={"usuario": "joao", "senha": "segredo123"})
    resp = cliente.get("/")
    assert "joao" in resp.get_data(as_text=True)


def test_pagina_principal_sem_login_nao_mostra_botao_de_sair(app_module):
    """Sem APP_USERS configurada (uso local), não existe sessão/usuário —
    o botão de sair não deve aparecer (não há nada de onde sair)."""
    mod = app_module(usuarios=None)
    cliente = mod.app.test_client()
    resp = cliente.get("/")
    assert "btnSair" not in resp.get_data(as_text=True)
