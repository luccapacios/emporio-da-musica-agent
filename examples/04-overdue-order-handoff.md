# Order status: overdue delivery and human handoff

**What it shows:** Order lookup with verification, detection that the delivery is past its ETA, and a logged handoff with protocol, SLA and the team's next opening time.

Run on 2026-11-03 at 01:16 (Campo Grande time) with `gemini-3.5-flash-lite`. Customer lines are scripted; Duda's replies and tool calls are the agent's real output. The conversation is in Brazilian Portuguese, the store's customer language, unless the customer writes in another language.

---

**Customer:** Oi, meu pedido ainda não chegou

> Tools: `get_policy(topic="shipping")`

**Duda:** Olá! Sou a Duda, assistente virtual da Empório da Música. Para eu verificar a situação da sua entrega, por favor, me informe o número do pedido junto com o seu telefone ou e-mail cadastrado.   
  
- Número do pedido  
- Telefone ou e-mail cadastrados

**Customer:** Pedido 8. E-mail anacarol.ferreira@coldmail.com

> Tools: `get_order(order_id=8, email="anacarol.ferreira@coldmail.com")`, `escalate_to_human(reason="overdue_delivery", customer_name="Ana", order_id=8, summary="Pedido 8 de Ana (anacarol.ferreira@coldmail.com) está atrasado desde 28/02/2026.")`

**Duda:** Ana, peço muitas desculpas pelo atraso, pois o seu pedido **8** consta como enviado com prazo estimado para **28/02/2026**.   
  
- Já encaminhei o seu caso para nossa equipe de atendimento sob o protocolo **EMP-20261103-0001**, com retorno em até 24 horas úteis (nossa equipe retorna na terça-feira, 03/11 às 09:00).  
- Código de rastreamento: `BRJL5544332BR` (você pode consultar no site dos Correios ou da Jadlog).

**Customer:** Obrigada! Vocês me avisam por e-mail?

**Duda:** Sim, Ana, a nossa equipe entrará em contato com você pelo e-mail cadastrado assim que houver uma atualização sobre a entrega.   
  
Tem mais alguma dúvida em que eu possa ajudar?
