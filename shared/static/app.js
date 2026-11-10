(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  // Same format as the server: 12 500,00 MT
  const mzn = (v) => {
    const [int, dec] = Number(v).toFixed(2).split(".");
    return int.replace(/\B(?=(\d{3})+(?!\d))/g, " ") + "," + dec + " MT";
  };

  // Show/hide destination, return-date and hours fields by trip type.
  function bindTripType(form) {
    const sync = () => {
      const t = $('[name="tipo"]:checked', form) || $('select[name="tipo"]', form);
      form.dataset.tipo = t ? t.value : "ida_volta";
      const dest = $('[name="destino"]', form);
      if (dest) dest.required = form.dataset.tipo !== "local";
    };
    form.addEventListener("change", (e) => { if (e.target.name === "tipo") sync(); });
    sync();
  }
  $$("form.search, #bookForm").forEach(bindTripType);

  // Live price on the vehicle page.
  const book = $("#bookForm");
  if (book) {
    let timer;
    const update = async () => {
      const fd = new FormData(book);
      const body = {
        origem: fd.get("origem"), destino: fd.get("destino") || fd.get("origem"), tipo: fd.get("tipo"),
        passageiros: Number(fd.get("passageiros") || 1), categoria: fd.get("categoria"),
        data: fd.get("data"), data_fim: book.dataset.tipo === "ida_volta" ? fd.get("data_fim") : "",
        horas: Number(fd.get("horas") || 0),
      };
      const box = $(".price-live", book);
      box.classList.add("loading");
      try {
        const r = await fetch("/api/cotacao", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const q = await r.json();
        if (r.ok) {
          $("#livePrice").textContent = mzn(q.total);
          $("#liveDeposit").textContent = `Sinal ${mzn(q.deposit)}` + (q.vehicles > 1 ? ` · ${q.vehicles} viaturas` : "");
        } else {
          $("#liveDeposit").textContent = q.erro || "Verifique os dados";
        }
      } catch { /* offline: keep last price */ }
      box.classList.remove("loading");
    };
    book.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(update, 350); });
    book.addEventListener("change", () => { clearTimeout(timer); timer = setTimeout(update, 50); });
  }

  // Ticket search: swap origin/destination and quick dates.
  $$("form.tsearch").forEach((f) => {
    $("[data-swap]", f)?.addEventListener("click", () => {
      const a = $('[name="de"]', f), b = $('[name="para"]', f);
      [a.value, b.value] = [b.value, a.value];
    });
    $$("[data-date]", f).forEach((btn) => btn.addEventListener("click", () => {
      const d = new Date();
      if (btn.dataset.date === "weekend") d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
      else d.setDate(d.getDate() + Number(btn.dataset.date));
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      $('[name="data"]', f).value = iso;
      if (f.reportValidity()) f.submit();
    }));
  });

  // Trip results: client-side filters and sorting.
  const list = $("#tripList");
  if (list) {
    const apply = () => {
      const want = {};
      $$("[data-filter]").forEach((g) => { want[g.dataset.filter] = $$("input:checked", g).map((i) => i.value); });
      let shown = 0;
      $$(".trip", list).forEach((t) => {
        const ok = Object.entries(want).every(([k, vals]) => !vals.length || vals.includes(t.dataset[k]));
        t.hidden = !ok; shown += ok;
      });
      $("#count").textContent = `${shown} autocarro(s)`;
      $("#noMatch").hidden = shown > 0 || !$$(".trip", list).length;
    };
    $$("[data-filter] input").forEach((i) => i.addEventListener("change", apply));
    $("#sortBy")?.addEventListener("change", (e) => {
      const k = e.target.value;
      const key = (t) => k === "depart" ? t.dataset.depart : k === "rating" ? -Number(t.dataset.rating) : Number(t.dataset[k]);
      $$(".trip", list).sort((a, b) => (key(a) > key(b)) - (key(a) < key(b))).forEach((t) => list.appendChild(t));
    });
  }

  // Seat map: selection limit, live total.
  const seatForm = $("#seatForm");
  if (seatForm) {
    const fare = Number(seatForm.dataset.fare), max = Number(seatForm.dataset.max);
    seatForm.addEventListener("change", (e) => {
      if (e.target.name !== "lugar") return;
      let chosen = $$('input[name="lugar"]:checked', seatForm);
      if (chosen.length > max) { e.target.checked = false; chosen = chosen.filter((i) => i !== e.target); alert(`Máximo de ${max} lugares por bilhete.`); }
      const nums = chosen.map((i) => i.value);
      $("#nSeats").textContent = nums.length;
      $("#seatTotal").textContent = mzn(nums.length * fare);
      $("#seatList").textContent = nums.length ? `Lugar(es): ${nums.join(", ")}` : `Toque nos lugares livres (máx. ${max}).`;
      $("#seatSubmit").disabled = !nums.length;
    });
  }

  // Checkout: seat-hold countdown.
  const hb = $(".holdbar");
  if (hb) {
    let left = Number(hb.dataset.left);
    const tick = () => {
      left = Math.max(0, left - 1);
      $("#holdTimer").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      hb.classList.toggle("urgent", left < 120);
      if (left === 0) { clearInterval(iv); hb.textContent = "O tempo terminou. Os lugares foram libertados — volte a escolher."; }
    };
    const iv = setInterval(tick, 1000);
  }

  // AI assistant chat widget.
  const chat = $("#chat");
  if (!chat) return;
  const panel = $("#chatPanel"), toggle = $(".chat-toggle", chat), log = $(".chat-log", chat), form = $(".chat-form", chat);
  const open = (v) => {
    panel.hidden = !v; toggle.setAttribute("aria-expanded", String(v));
    if (v) $("input", form).focus();
  };
  toggle.addEventListener("click", () => open(panel.hidden));
  $(".chat-close", chat).addEventListener("click", () => open(false));
  $$("[data-open-chat]").forEach((b) => b.addEventListener("click", () => open(true)));

  const add = (cls, text) => {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  };

  let busy = false;
  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    $(".chips", log)?.remove();
    add("me", text);
    const typing = add("bot typing", "a escrever…");
    try {
      const r = await fetch("/api/assistente", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mensagem: text }) });
      const data = await r.json();
      typing.remove();
      const m = add("bot", data.reply.replace(/\*\*(.+?)\*\*/g, "$1"));
      if (data.trips && data.trips.link_pesquisa) {
        const a = document.createElement("a");
        a.className = "quote-card";
        a.href = data.trips.link_pesquisa;
        a.textContent = `Ver ${data.trips.viagens.length} autocarro(s) e escolher lugar →`;
        m.appendChild(a);
      } else if (data.quote && data.quote.link_reserva) {
        const a = document.createElement("a");
        a.className = "quote-card";
        a.href = data.quote.link_reserva;
        a.textContent = `Ver viaturas · ${mzn(data.quote.total)} →`;
        m.appendChild(a);
      }
    } catch {
      typing.remove();
      add("bot", "Sem ligação. Tente de novo ou fale connosco pelo WhatsApp.");
    }
    busy = false;
    log.scrollTop = log.scrollHeight;
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); const i = $("input", form); send(i.value); i.value = ""; });
  log.addEventListener("click", (e) => { if (e.target.matches(".chips .chip")) send(e.target.textContent); });
})();
