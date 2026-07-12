from app.classify import classify


def test_real_reply() -> None:
    c = classify(
        "ceo@thc.com.br",
        "Re: Programa Builders Club",
        "Oi Bruno, gostei da proposta. Podemos falar quinta?",
        {},
    )
    assert c.category == "reply"


def test_ooo_headers() -> None:
    c = classify(
        "diretor@empresa.com.br",
        "Automatic reply: Programa Builders Club",
        "Estou fora do escritorio e retorno no dia 20/07.",
        {"Auto-Submitted": "auto-replied"},
    )
    assert c.category == "ooo"


def test_ooo_ptbr_body_without_headers() -> None:
    c = classify(
        "vp@empresa.com.br",
        "Re: Programa Builders Club",
        "Estou de ferias, sem acesso ao e-mail. Retorno em agosto.",
        {},
    )
    assert c.category == "ooo"


def test_ooo_ptbr_ausente() -> None:
    c = classify(
        "vp@empresa.com.br",
        "Resposta automatica",
        "Estarei ausente ate segunda-feira.",
        {},
    )
    assert c.category == "ooo"


def test_auto_reply_non_ooo() -> None:
    c = classify(
        "suporte@empresa.com.br",
        "Recebemos seu chamado #4821",
        "Seu ticket foi criado e sera respondido em breve.",
        {"Precedence": "auto_reply"},
    )
    assert c.category == "auto"


def test_optout_remover() -> None:
    c = classify(
        "cfo@empresa.com.br",
        "Re: Programa Builders Club",
        "Por favor remover meu email dessa lista.",
        {},
    )
    assert c.category == "optout"


def test_optout_descadastrar() -> None:
    c = classify(
        "cfo@empresa.com.br", "descadastrar", "descadastrar", {}
    )
    assert c.category == "optout"


def test_optout_nao_quero() -> None:
    c = classify(
        "cfo@empresa.com.br",
        "Re: contato",
        "Nao quero receber esses emails.",
        {},
    )
    assert c.category == "optout"


def test_optout_parar() -> None:
    c = classify(
        "cfo@empresa.com.br", "Re: contato", "Favor parar de enviar.", {}
    )
    assert c.category == "optout"


def test_optout_beats_interest_wording() -> None:
    c = classify(
        "cfo@empresa.com.br",
        "Re: Programa",
        "Obrigado, mas nao tenho interesse. Unsubscribe.",
        {},
    )
    assert c.category == "optout"


def test_bounce_mailer_daemon() -> None:
    c = classify(
        "MAILER-DAEMON@mx.google.com",
        "Delivery Status Notification (Failure)",
        "address not found",
        {},
    )
    assert c.category == "bounce"


def test_reply_with_ferias_in_signature_is_not_flagged_when_deep() -> None:
    # 'ferias' appears past the 4000-char scan window: still a real reply.
    body = "Topo da resposta, vamos conversar sim." + ("x" * 4100) + " ferias"
    c = classify("ceo@thc.com.br", "Re: Programa", body, {})
    assert c.category == "reply"


def test_reply_keeps_priority_over_polite_no() -> None:
    c = classify(
        "ceo@thc.com.br",
        "Re: Programa",
        "Agora nao e o momento, volte a falar em setembro.",
        {},
    )
    assert c.category == "reply"
