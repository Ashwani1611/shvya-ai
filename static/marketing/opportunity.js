(function () {
  const ids = ['leadCount', 'dealValue', 'recoveryRate', 'closeRate'];
  const fields = ids.map(id => document.getElementById(id));
  if (fields.some(field => !field)) return;
  const money = new Intl.NumberFormat('en-IN', {style:'currency', currency:'INR', maximumFractionDigits:0});
  const number = new Intl.NumberFormat('en-IN', {maximumFractionDigits:1});
  const examples = {
    services: [500,10000,'A prospect asks for a quote, then goes quiet. A timely follow-up can restart the conversation.'],
    education: [300,25000,'A parent asks about your course. Keep the next counselling conversation from slipping through the cracks.'],
    retail: [1000,2500,'A shopper enquires on WhatsApp but does not order. Explore the value of restarting that conversation.'],
    b2b: [100,100000,'A buyer requests a quotation. Keep specifications, follow-ups and the next action connected.'],
    property: [200,50000,'A buyer delays a site visit. Use your actual fee or commission per closed deal, rather than the property price.']
  };
  function update() {
    const values = fields.map((field,i) => {
      const value = Number(field.value);
      return Number.isFinite(value) ? Math.min(i > 1 ? 100 : 1000000000, Math.max(0,value)) : 0;
    });
    const [leads,deal,recovery,close] = values;
    const recovered = leads * recovery / 100;
    const sales = recovered * close / 100;
    const monthly = sales * deal;
    document.getElementById('recoveryLabel').textContent = recovery + '%';
    document.getElementById('closeLabel').textContent = close + '%';
    document.getElementById('monthlyValue').textContent = money.format(monthly);
    document.getElementById('annualValue').textContent = money.format(monthly * 12);
    document.getElementById('calcExplanation').textContent = `${number.format(leads)} enquiries × ${recovery}% recovered = ${number.format(recovered)} conversations. At a ${close}% close rate, that is ${number.format(sales)} potential sales × ${money.format(deal)}.`;
    const cta = document.getElementById('calcBooking');
    const summary = `Scenario: ${leads} enquiries/month, ${money.format(deal)}/sale, ${recovery}% recovery, ${close}% close rate`;
    cta.href = '/book-a-call/?interest=' + encodeURIComponent(summary);
  }
  fields.forEach(field => field.addEventListener('input',update));
  document.getElementById('businessExample').addEventListener('change',event => {
    const [leads,deal,context] = examples[event.target.value];
    fields[0].value = leads; fields[1].value = deal;
    document.getElementById('businessContext').textContent = context;
    update();
  });
  update();
})();
