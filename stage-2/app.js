const app = document.querySelector('#app');
const authNav = document.querySelector('#auth-nav');
const session = () => { try { return JSON.parse(localStorage.getItem('tablekeeper-session') || 'null'); } catch { return null; } };
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const api = async (path, options = {}) => {
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (session()?.token) headers.Authorization = `Bearer ${session().token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(data.error?.message || 'Não foi possível concluir.'); error.code = data.error?.code; error.status = response.status; throw error; }
  return data;
};
const notice = (id, text, style='error') => `<div class="notice ${style}" data-testid="${id}" role="${style === 'error' ? 'alert' : 'status'}">${esc(text)}</div>`;
function nav() {
  const user = session();
  authNav.innerHTML = user ? `<span data-testid="current-user">Olá, ${esc(user.display_name)}</span> <button class="secondary" data-testid="logout-button" id="logout">Sair</button>` : `<a href="/login">Entrar</a> <a href="/signup">Criar conta</a>`;
  document.querySelector('#logout')?.addEventListener('click', () => { localStorage.removeItem('tablekeeper-session'); location.href = '/'; });
}
function authPage(kind) {
  const signup = kind === 'signup';
  app.innerHTML = `<section class="hero"><div class="eyebrow">Bem-vindo à mesa</div><h1>${signup ? 'Crie sua conta' : 'Entre para reservar'}</h1><p>${signup ? 'Uma boa noite começa aqui.' : 'Suas próximas experiências estão a poucos passos.'}</p></section><section class="panel"><h2>${signup ? 'Seus dados' : 'Acesse sua conta'}</h2><form id="auth-form" class="stack"><label class="field">E-mail<input data-testid="${kind}-email" type="email" required autocomplete="email"></label>${signup ? `<label class="field">Nome<input data-testid="signup-display-name" required autocomplete="name"></label>` : ''}<label class="field">Senha<input data-testid="${kind}-password" type="password" required autocomplete="${signup ? 'new-password' : 'current-password'}"></label><button class="primary" data-testid="${kind}-submit">${signup ? 'Criar conta' : 'Entrar'}</button></form><div id="auth-feedback"></div><p class="hint">${signup ? 'Já tem conta? <a href="/login">Entre aqui</a>.' : 'Ainda não tem conta? <a href="/signup">Cadastre-se</a>.'}</p></section>`;
  document.querySelector('#auth-form').addEventListener('submit', async event => {
    event.preventDefault(); const email = document.querySelector(`[data-testid="${kind}-email"]`).value;
    const password = document.querySelector(`[data-testid="${kind}-password"]`).value;
    const body = { email, password }; if (signup) body.display_name = document.querySelector('[data-testid="signup-display-name"]').value;
    try { const result = await api(`/auth/${kind}`, { method:'POST', body:JSON.stringify(body) }); localStorage.setItem('tablekeeper-session', JSON.stringify(result)); location.href = '/'; }
    catch (error) { document.querySelector('#auth-feedback').innerHTML = notice('auth-error', error.message); }
  });
}
let restaurants = [], currentRestaurant = null, lastQuery = null, searchVersion = 0, selection = null, pending = null;
const labels = ids => ids.map(id => currentRestaurant?.tables.find(table => table.id === id)?.label || id).join(' + ');
async function home() {
  app.innerHTML = `<section class="hero"><div class="eyebrow">À sua mesa</div><h1>Encontre o lugar para um encontro especial.</h1><p>Escolha o restaurante, a data e o tamanho do seu grupo. Nós mostramos as mesas que esperam por você.</p></section><section class="panel"><h2>Encontre sua mesa</h2><form id="search-form" class="fields"><label class="field">Restaurante<select data-testid="restaurant-select" required></select></label><label class="field">Data<input data-testid="date-input" type="date" required></label><label class="field">Pessoas<input data-testid="party-size-input" type="number" min="1" value="2" required></label><button class="primary" data-testid="search-button">Buscar mesas</button></form><div id="search-feedback"></div></section><section class="panel"><div class="section-head"><div><div class="eyebrow">Disponibilidade</div><h2>Escolha seu horário</h2></div><p>Selecione uma mesa disponível para reservar.</p></div><div id="results" class="hint">Escolha seus critérios e busque.</div></section><div id="booking-area"></div>`;
  document.querySelector('[data-testid="date-input"]').value = new Date().toLocaleDateString('en-CA');
  document.querySelector('#search-form').addEventListener('submit', event => { event.preventDefault(); search(false); });
  try { restaurants = (await api('/restaurants')).restaurants; const select = document.querySelector('[data-testid="restaurant-select"]'); select.innerHTML = restaurants.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join(''); if (!restaurants.length) document.querySelector('#results').textContent = 'Ainda não há restaurantes disponíveis.'; }
  catch { document.querySelector('#results').textContent = 'Não foi possível carregar os restaurantes. Tente novamente.'; }
}
async function search(preserve, query = null) {
  const version = ++searchVersion;
  if (!query) query = { restaurant_id:document.querySelector('[data-testid="restaurant-select"]').value, date:document.querySelector('[data-testid="date-input"]').value, party_size:document.querySelector('[data-testid="party-size-input"]').value };
  if (!preserve) { selection = null; pending = null; document.querySelector('#booking-area').innerHTML = ''; }
  lastQuery = query;
  const results = document.querySelector('#results'); results.innerHTML = '<p role="status">Buscando horários disponíveis…</p>';
  document.querySelector('#search-feedback').innerHTML = '';
  try {
    const [restaurant, availability] = await Promise.all([api(`/restaurants/${encodeURIComponent(query.restaurant_id)}`), api(`/availability?${new URLSearchParams(query)}`)]);
    if (version !== searchVersion) return;
    currentRestaurant = restaurant;
    drawGrid(availability, restaurant, Number(query.party_size));
  } catch (error) { if (version === searchVersion) results.innerHTML = notice('search-error', error.message); }
}
function drawGrid(data, restaurant, party) {
  const results = document.querySelector('#results');
  if (!data.slots.length) { results.innerHTML = '<div data-testid="no-slots" class="notice uncertain">Não há horários neste dia. Experimente outra data.</div>'; return; }
  const slots = data.slots;
  const options = [...restaurant.tables.map(table => ({ ids:[table.id], capacity:table.capacity })), ...(restaurant.combinable || []).map(pair => ({ ids:pair, capacity:pair.reduce((n,id) => n + restaurant.tables.find(t => t.id === id).capacity, 0) }))].filter(option => option.ids.length === 1 || option.capacity >= party);
  const head = slots.map(slot => `<th scope="col">${esc(slot.starts_at_local.slice(11))}</th>`).join('');
  const rows = options.map(option => `<tr><th scope="row">${esc(labels(option.ids))}<br><small class="muted">Até ${option.capacity} pessoas</small></th>${slots.map(slot => {
    const available = slot.available_options.some(candidate => candidate.table_ids.length === option.ids.length && candidate.table_ids.every((id, i) => id === option.ids[i]));
    const id = `slot-${option.ids.join('+')}-${slot.starts_at_local.slice(11)}`;
    return `<td><button class="slot" data-testid="${esc(id)}" data-available="${available}" data-ids="${esc(option.ids.join(','))}" data-start="${esc(slot.starts_at_local)}" ${available ? '' : 'disabled'} aria-label="${esc(labels(option.ids))}, ${esc(slot.starts_at_local.slice(11))}, ${available ? 'disponível' : 'indisponível'}">${available ? 'Reservar' : 'Ocupada'}</button></td>`;
  }).join('')}</tr>`).join('');
  results.innerHTML = `<div class="grid-wrap" data-testid="availability-grid"><table class="grid"><thead><tr><th scope="col">Mesas</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
  results.querySelectorAll('.slot:not(:disabled)').forEach(button => button.addEventListener('click', () => {
    if (!session()) { document.querySelector('#search-feedback').innerHTML = notice('auth-error', 'Entre na sua conta para reservar.'); return; }
    results.querySelectorAll('.slot').forEach(cell => cell.classList.remove('selected'));
    button.classList.add('selected');
    selection = { restaurant_id:restaurant.id, table_ids:button.dataset.ids.split(','), starts_at_local:button.dataset.start };
    pending = null; bookingForm(party);
  }));
}
function bookingForm(party) {
  const name = restaurants.find(r => r.id === selection.restaurant_id)?.name || currentRestaurant.name;
  const area = document.querySelector('#booking-area');
  area.innerHTML = `<div class="booking-layout"><section class="panel" data-testid="booking-form"><div class="eyebrow">Sua escolha</div><h2>Finalize sua reserva</h2><p data-testid="booking-summary">${esc(labels(selection.table_ids))} · ${esc(selection.starts_at_local.slice(11))}, ${esc(selection.starts_at_local.slice(0,10))}</p><form id="booking-form-fields" class="stack"><label class="field">Pessoas<input data-testid="booking-party-size" type="number" min="1" required value="${party}"></label><button class="primary" data-testid="booking-submit">Confirmar reserva</button></form><div id="booking-feedback"></div></section><div id="confirmation-area"></div></div>`;
  document.querySelector('#booking-form-fields').addEventListener('submit', async event => {
    event.preventDefault();
    const body = { restaurant_id:selection.restaurant_id, table_ids:[...selection.table_ids], starts_at_local:selection.starts_at_local, party_size:Number(document.querySelector('[data-testid="booking-party-size"]').value) };
    const serialized = JSON.stringify(body);
    if (!pending || pending.serialized !== serialized) pending = { key:crypto.randomUUID(), serialized, body };
    const request = pending;
    const feedback = document.querySelector('#booking-feedback');
    feedback.innerHTML = '<p class="hint" role="status">Confirmando sua mesa…</p>';
    try {
      const reservation = await api('/reservations', { method:'POST', headers:{'Idempotency-Key':request.key}, body:request.serialized });
      if (request !== pending) return;
      feedback.innerHTML = ''; const tableNames = labels(reservation.table_ids);
      document.querySelector('#confirmation-area').innerHTML = `<section class="panel success" data-testid="confirmation"><div class="eyebrow">Reserva confirmada</div><h2>Sua mesa está esperando.</h2><p>Guarde sua referência:</p><p class="reference" data-testid="confirmation-reference">${esc(reservation.reference)}</p><p data-testid="confirmation-details">${esc(name)} · ${esc(tableNames)} · ${esc(reservation.starts_at_local)}</p><p data-testid="confirmation-tables">${esc(tableNames)}</p><a href="/lookup">Consultar reserva</a></section>`;
    } catch (error) {
      if (request !== pending) return;
      document.querySelector('#confirmation-area').innerHTML = '';
      if (error.status) {
        feedback.innerHTML = notice('booking-error', error.code === 'table_unavailable' ? 'Essa mesa acabou de ser reservada. Escolha outra opção disponível.' : error.message);
        pending = null;
        if (error.code === 'table_unavailable') search(true, lastQuery);
      } else feedback.innerHTML = notice('booking-uncertain', 'Não sabemos se a reserva foi concluída. Tente novamente para recuperar a confirmação.', 'uncertain');
    }
  });
}
function lookup() {
  app.innerHTML = `<section class="hero"><div class="eyebrow">Sua reserva</div><h1>Consulte sua experiência.</h1><p>Informe a referência recebida na confirmação para ver os detalhes ou cancelar.</p></section><section class="panel"><form id="lookup-form" class="stack"><label class="field">Referência da reserva<input data-testid="lookup-reference-input" required autocomplete="off"></label><button class="primary" data-testid="lookup-submit">Consultar reserva</button></form><div id="lookup-feedback"></div><div id="lookup-detail"></div></section>`;
  document.querySelector('#lookup-form').addEventListener('submit', async event => {
    event.preventDefault(); const ref = document.querySelector('[data-testid="lookup-reference-input"]').value.trim();
    const feedback = document.querySelector('#lookup-feedback'); feedback.innerHTML = '';
    try { renderReservation(await api(`/reservations/${encodeURIComponent(ref)}`)); }
    catch (error) { document.querySelector('#lookup-detail').innerHTML = ''; feedback.innerHTML = notice('reservation-error', error.status === 401 ? 'Entre na sua conta para consultar esta reserva.' : error.message); }
  });
}
async function renderReservation(reservation) {
  const restaurant = await api(`/restaurants/${encodeURIComponent(reservation.restaurant_id)}`);
  const tableNames = reservation.table_ids.map(id => restaurant.tables.find(t => t.id === id)?.label || id).join(' + ');
  const detail = document.querySelector('#lookup-detail');
  detail.innerHTML = `<section class="panel" data-testid="reservation-detail"><div class="eyebrow">${esc(restaurant.name)}</div><h2>Reserva ${esc(reservation.reference)}</h2><p data-testid="reservation-tables">${esc(tableNames)}</p><p>${esc(reservation.starts_at_local)}</p><p>Status: <strong data-testid="reservation-status">${esc(reservation.status)}</strong></p>${reservation.status === 'confirmed' ? '<button class="secondary" data-testid="reservation-cancel-button">Cancelar reserva</button>' : ''}</section>`;
  detail.querySelector('[data-testid="reservation-cancel-button"]')?.addEventListener('click', async () => {
    try { const changed = await api(`/reservations/${encodeURIComponent(reservation.reference)}/cancel`, { method:'POST' }); document.querySelector('#lookup-feedback').innerHTML = ''; renderReservation(changed); }
    catch (error) { document.querySelector('#lookup-feedback').innerHTML = notice('reservation-error', error.message); }
  });
}
nav();
if (location.pathname === '/signup') authPage('signup'); else if (location.pathname === '/login') authPage('login'); else if (location.pathname === '/lookup') lookup(); else home();
