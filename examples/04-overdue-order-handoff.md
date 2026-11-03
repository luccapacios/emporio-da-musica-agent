# Order status: overdue delivery and human handoff

**What it shows:** Order lookup with verification, detection that the delivery is past its ETA, and a logged handoff with protocol, SLA and the team's next opening time.

Run on 03/11/2026 at 00:28 (Campo Grande time) with `gemini-3.5-flash-lite`. Customer lines are scripted; Duda's replies and tool calls are the agent's real output.

---

**Customer:** Oi, meu pedido ainda não chegou

**Duda:** Para eu dar uma olhada em como está a entrega, você pode me informar o número do pedido junto com o seu e-mail ou telefone cadastrados? 🎸

**Customer:** Pedido 8. E-mail anacarol.ferreira@coldmail.com

> Tools: `get_order(order_id=8, email="anacarol.ferreira@coldmail.com")`, `escalate_to_human(summary="Pedido 8 com entrega atrasada (atraso de 248 dias em relação à estimativa de fevereiro).", order_id=8, customer_name="Ana", contact="anacarol.ferreira@coldmail.com", reason="overdue_delivery")`

**Duda:** Oi, Ana! Sinto muito por isso, o sistema mostra que o seu pedido realmente está com a entrega atrasada.   
  
Já acionei a nossa equipe com o protocolo **EMP-20261103-0001** e entraremos em contato para verificar o que aconteceu com a transportadora. Como nossa equipe não está no momento, retornaremos a partir de **terça-feira, 03/11 às 09:00**, em até 24 horas úteis.

**Customer:** Pode sim, por favor

**Duda:** Combinado, Ana! Já está tudo registrado com a nossa equipe e vamos te dar um retorno o mais rápido possível.   
  
Há mais alguma coisa em que eu possa te ajudar hoje?
