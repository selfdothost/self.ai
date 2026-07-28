var pi = Object.defineProperty;
var $r = (t) => {
  throw TypeError(t);
};
var _i = (t, e, n) => e in t ? pi(t, e, { enumerable: !0, configurable: !0, writable: !0, value: n }) : t[e] = n;
var O = (t, e, n) => _i(t, typeof e != "symbol" ? e + "" : e, n), Nn = (t, e, n) => e.has(t) || $r("Cannot " + n);
var f = (t, e, n) => (Nn(t, e, "read from private field"), n ? n.call(t) : e.get(t)), w = (t, e, n) => e.has(t) ? $r("Cannot add the same private member more than once") : e instanceof WeakSet ? e.add(t) : e.set(t, n), y = (t, e, n, r) => (Nn(t, e, "write to private field"), r ? r.call(t, n) : e.set(t, n), n), k = (t, e, n) => (Nn(t, e, "access private method"), n);
var Yr;
typeof window < "u" && ((Yr = window.__svelte ?? (window.__svelte = {})).v ?? (Yr.v = /* @__PURE__ */ new Set())).add("5");
const vi = 2, Kr = "[", Wr = "[!", Sr = "[?", Jr = "]", mt = {}, D = Symbol("uninitialized"), yi = "http://www.w3.org/1999/xhtml", Xr = !1;
var gi = Array.isArray, mi = Array.prototype.indexOf, un = Array.prototype.includes, wi = Array.from, hn = Object.keys, dn = Object.defineProperty, lt = Object.getOwnPropertyDescriptor, bi = Object.getOwnPropertyDescriptors, Ei = Object.prototype, ki = Array.prototype, Qr = Object.getPrototypeOf, Ar = Object.isExtensible;
const Ti = () => {
};
function $i(t) {
  for (var e = 0; e < t.length; e++)
    t[e]();
}
function Gr() {
  var t, e, n = new Promise((r, s) => {
    t = r, e = s;
  });
  return { promise: n, resolve: t, reject: e };
}
const M = 2, wt = 4, kn = 8, Zr = 1 << 24, ue = 16, pe = 32, ke = 64, Un = 128, re = 512, I = 1024, q = 2048, de = 4096, se = 8192, ie = 16384, rt = 32768, Or = 1 << 25, bt = 65536, pn = 1 << 17, Si = 1 << 18, st = 1 << 19, Ai = 1 << 20, tt = 65536, _n = 1 << 21, ct = 1 << 22, Fe = 1 << 23, Bn = Symbol("$state"), Oi = Symbol("legacy props"), Gt = Symbol("attributes"), Vn = Symbol("class"), Ri = Symbol("style"), At = Symbol("text"), jt = new class extends Error {
  constructor() {
    super(...arguments);
    O(this, "name", "StaleReactionError");
    O(this, "message", "The reaction that called `getAbortSignal()` was re-run or destroyed");
  }
}(), ir = 3, Tn = 8;
function es(t) {
  throw new Error("https://svelte.dev/e/lifecycle_outside_component");
}
function xi() {
  throw new Error("https://svelte.dev/e/async_derived_orphan");
}
function Ci(t) {
  throw new Error("https://svelte.dev/e/effect_in_teardown");
}
function Ni() {
  throw new Error("https://svelte.dev/e/effect_in_unowned_derived");
}
function Bi(t) {
  throw new Error("https://svelte.dev/e/effect_orphan");
}
function Li() {
  throw new Error("https://svelte.dev/e/effect_update_depth_exceeded");
}
function qi() {
  throw new Error("https://svelte.dev/e/hydration_failed");
}
function Pi() {
  throw new Error("https://svelte.dev/e/state_descriptors_fixed");
}
function Di() {
  throw new Error("https://svelte.dev/e/state_prototype_fixed");
}
function Ii() {
  throw new Error("https://svelte.dev/e/state_unsafe_mutation");
}
function Mi() {
  throw new Error("https://svelte.dev/e/svelte_boundary_reset_onerror");
}
function Fi() {
  console.warn("https://svelte.dev/e/derived_inert");
}
function $n(t) {
  console.warn("https://svelte.dev/e/hydration_mismatch");
}
function Ui() {
  console.warn("https://svelte.dev/e/svelte_boundary_reset_noop");
}
let B = !1;
function at(t) {
  B = t;
}
let N;
function Te(t) {
  if (t === null)
    throw $n(), mt;
  return N = t;
}
function or() {
  return Te(/* @__PURE__ */ He(N));
}
function ye(t) {
  if (B) {
    if (/* @__PURE__ */ He(N) !== null)
      throw $n(), mt;
    N = t;
  }
}
function Vi(t = 1) {
  if (B) {
    for (var e = t, n = N; e--; )
      n = /** @type {TemplateNode} */
      /* @__PURE__ */ He(n);
    N = n;
  }
}
function ts(t = !0) {
  for (var e = 0, n = N; ; ) {
    if (n.nodeType === Tn) {
      var r = (
        /** @type {Comment} */
        n.data
      );
      if (r === Jr) {
        if (e === 0) return n;
        e -= 1;
      } else (r === Kr || r === Wr || // "[1", "[2", etc. for if blocks
      r[0] === "[" && !isNaN(Number(r.slice(1)))) && (e += 1);
    }
    var s = (
      /** @type {TemplateNode} */
      /* @__PURE__ */ He(n)
    );
    t && n.remove(), n = s;
  }
}
function Hi(t) {
  if (!t || t.nodeType !== Tn)
    throw $n(), mt;
  return (
    /** @type {Comment} */
    t.data
  );
}
function ns(t) {
  return t === this.v;
}
function ji(t, e) {
  return t != t ? e == e : t !== e || t !== null && typeof t == "object" || typeof t == "function";
}
function Yi(t) {
  return !ji(t, this.v);
}
let zi = !1, U = null;
function Et(t) {
  U = t;
}
function rs(t, e = !1, n) {
  U = {
    p: U,
    i: !1,
    c: null,
    e: null,
    s: t,
    x: null,
    r: (
      /** @type {Effect} */
      E
    ),
    l: null
  };
}
function ss(t) {
  var e = (
    /** @type {ComponentContext} */
    U
  ), n = e.e;
  if (n !== null) {
    e.e = null;
    for (var r of n)
      As(r);
  }
  return t !== void 0 && (e.x = t), e.i = !0, U = e.p, t ?? /** @type {T} */
  {};
}
function is() {
  return !0;
}
let Ye = [];
function os() {
  var t = Ye;
  Ye = [], $i(t);
}
function ut(t) {
  if (Ye.length === 0 && !Lt) {
    var e = Ye;
    queueMicrotask(() => {
      e === Ye && os();
    });
  }
  Ye.push(t);
}
function Ki() {
  for (; Ye.length > 0; )
    os();
}
function as(t) {
  var e = E;
  if (e === null)
    return b.f |= Fe, t;
  if ((e.f & rt) === 0 && (e.f & wt) === 0)
    throw t;
  Me(t, e);
}
function Me(t, e) {
  if (!(e !== null && (e.f & ie) !== 0)) {
    for (; e !== null; ) {
      if ((e.f & Un) !== 0) {
        if ((e.f & rt) === 0)
          throw t;
        try {
          e.b.error(t);
          return;
        } catch (n) {
          t = n;
        }
      }
      e = e.parent;
    }
    throw t;
  }
}
const Wi = -7169;
function C(t, e) {
  t.f = t.f & Wi | e;
}
function ar(t) {
  (t.f & re) !== 0 || t.deps === null ? C(t, I) : C(t, de);
}
function fs(t) {
  if (t !== null)
    for (const e of t)
      (e.f & M) === 0 || (e.f & tt) === 0 || (e.f ^= tt, fs(
        /** @type {Derived} */
        e.deps
      ));
}
function ls(t, e, n) {
  (t.f & q) !== 0 ? e.add(t) : (t.f & de) !== 0 && n.add(t), fs(t.deps), C(t, I);
}
function Sn(t) {
  var e = b, n = E;
  oe(null), $e(null);
  try {
    return t();
  } finally {
    oe(e), $e(n);
  }
}
function Ji(t) {
  let e = 0, n = Yt(0), r;
  return () => {
    dr() && (x(n), Os(() => (e === 0 && (r = _r(() => t(() => qt(n)))), e += 1, () => {
      ut(() => {
        e -= 1, e === 0 && (r == null || r(), r = void 0, qt(n));
      });
    })));
  };
}
var Xi = bt | st;
function Qi(t, e, n, r) {
  new Gi(t, e, n, r);
}
var W, It, Z, We, H, ee, V, J, xe, Je, De, ht, Mt, Ft, Ce, wn, A, cs, us, hs, Hn, Zt, en, jn, Yn;
class Gi {
  /**
   * @param {TemplateNode} node
   * @param {BoundaryProps} props
   * @param {((anchor: Node) => void)} children
   * @param {((error: unknown) => unknown) | undefined} [transform_error]
   */
  constructor(e, n, r, s) {
    w(this, A);
    /** @type {Boundary | null} */
    O(this, "parent");
    O(this, "is_pending", !1);
    /**
     * API-level transformError transform function. Transforms errors before they reach the `failed` snippet.
     * Inherited from parent boundary, or defaults to identity.
     * @type {(error: unknown) => unknown}
     */
    O(this, "transform_error");
    /** @type {TemplateNode} */
    w(this, W);
    /** @type {TemplateNode | null} */
    w(this, It, B ? N : null);
    /** @type {BoundaryProps} */
    w(this, Z);
    /** @type {((anchor: Node) => void)} */
    w(this, We);
    /** @type {Effect} */
    w(this, H);
    /** @type {Effect | null} */
    w(this, ee, null);
    /** @type {Effect | null} */
    w(this, V, null);
    /** @type {Effect | null} */
    w(this, J, null);
    /** @type {DocumentFragment | null} */
    w(this, xe, null);
    w(this, Je, 0);
    w(this, De, 0);
    w(this, ht, !1);
    /** @type {Set<Effect>} */
    w(this, Mt, /* @__PURE__ */ new Set());
    /** @type {Set<Effect>} */
    w(this, Ft, /* @__PURE__ */ new Set());
    /**
     * A source containing the number of pending async deriveds/expressions.
     * Only created if `$effect.pending()` is used inside the boundary,
     * otherwise updating the source results in needless `Batch.ensure()`
     * calls followed by no-op flushes
     * @type {Source<number> | null}
     */
    w(this, Ce, null);
    w(this, wn, Ji(() => (y(this, Ce, Yt(f(this, Je))), () => {
      y(this, Ce, null);
    })));
    var i;
    y(this, W, e), y(this, Z, n), y(this, We, (o) => {
      var a = (
        /** @type {Effect} */
        E
      );
      a.b = this, a.f |= Un, r(o);
    }), this.parent = /** @type {Effect} */
    E.b, this.transform_error = s ?? ((i = this.parent) == null ? void 0 : i.transform_error) ?? ((o) => o), y(this, H, Rs(() => {
      if (B) {
        const o = (
          /** @type {Comment} */
          f(this, It)
        );
        or();
        const a = o.data === Wr;
        if (o.data.startsWith(Sr)) {
          const c = JSON.parse(o.data.slice(Sr.length));
          k(this, A, us).call(this, c);
        } else a ? k(this, A, hs).call(this) : k(this, A, cs).call(this);
      } else
        k(this, A, Hn).call(this);
    }, Xi)), B && y(this, W, N);
  }
  /**
   * Defer an effect inside a pending boundary until the boundary resolves
   * @param {Effect} effect
   */
  defer_effect(e) {
    ls(e, f(this, Mt), f(this, Ft));
  }
  /**
   * Returns `false` if the effect exists inside a boundary whose pending snippet is shown
   * @returns {boolean}
   */
  is_rendered() {
    return !this.is_pending && (!this.parent || this.parent.is_rendered());
  }
  has_pending_snippet() {
    return !!f(this, Z).pending;
  }
  /**
   * Update the source that powers `$effect.pending()` inside this boundary,
   * and controls when the current `pending` snippet (if any) is removed.
   * Do not call from inside the class
   * @param {1 | -1} d
   * @param {Batch} batch
   */
  update_pending_count(e, n) {
    k(this, A, jn).call(this, e, n), y(this, Je, f(this, Je) + e), !(!f(this, Ce) || f(this, ht)) && (y(this, ht, !0), ut(() => {
      y(this, ht, !1), f(this, Ce) && gn(f(this, Ce), f(this, Je));
    }));
  }
  get_effect_pending() {
    return f(this, wn).call(this), x(
      /** @type {Source<number>} */
      f(this, Ce)
    );
  }
  /** @param {unknown} error */
  error(e) {
    if (!f(this, Z).onerror && !f(this, Z).failed)
      throw e;
    m != null && m.is_fork ? (f(this, ee) && m.skip_effect(f(this, ee)), f(this, V) && m.skip_effect(f(this, V)), f(this, J) && m.skip_effect(f(this, J)), m.oncommit(() => {
      k(this, A, Yn).call(this, e);
    })) : k(this, A, Yn).call(this, e);
  }
}
W = new WeakMap(), It = new WeakMap(), Z = new WeakMap(), We = new WeakMap(), H = new WeakMap(), ee = new WeakMap(), V = new WeakMap(), J = new WeakMap(), xe = new WeakMap(), Je = new WeakMap(), De = new WeakMap(), ht = new WeakMap(), Mt = new WeakMap(), Ft = new WeakMap(), Ce = new WeakMap(), wn = new WeakMap(), A = new WeakSet(), cs = function() {
  try {
    y(this, ee, Re(() => f(this, We).call(this, f(this, W))));
  } catch (e) {
    this.error(e);
  }
}, /**
 * @param {unknown} error The deserialized error from the server's hydration comment
 */
us = function(e) {
  const n = f(this, Z).failed;
  n && y(this, J, Re(() => {
    n(
      f(this, W),
      () => e,
      () => () => {
      }
    );
  }));
}, hs = function() {
  const e = f(this, Z).pending;
  e && (this.is_pending = !0, y(this, V, Re(() => e(f(this, W)))), ut(() => {
    var n = y(this, xe, document.createDocumentFragment()), r = nt();
    n.append(r), y(this, ee, k(this, A, en).call(this, () => Re(() => f(this, We).call(this, r)))), f(this, De) === 0 && (f(this, W).before(n), y(this, xe, null), Pt(
      /** @type {Effect} */
      f(this, V),
      () => {
        y(this, V, null);
      }
    ), k(this, A, Zt).call(
      this,
      /** @type {Batch} */
      m
    ));
  }));
}, Hn = function() {
  try {
    if (this.is_pending = this.has_pending_snippet(), y(this, De, 0), y(this, Je, 0), y(this, ee, Re(() => {
      f(this, We).call(this, f(this, W));
    })), f(this, De) > 0) {
      var e = y(this, xe, document.createDocumentFragment());
      Ls(f(this, ee), e);
      const n = (
        /** @type {(anchor: Node) => void} */
        f(this, Z).pending
      );
      y(this, V, Re(() => n(f(this, W))));
    } else
      k(this, A, Zt).call(
        this,
        /** @type {Batch} */
        m
      );
  } catch (n) {
    this.error(n);
  }
}, /**
 * @param {Batch} batch
 */
Zt = function(e) {
  this.is_pending = !1, e.transfer_effects(f(this, Mt), f(this, Ft));
}, /**
 * @template T
 * @param {() => T} fn
 */
en = function(e) {
  var n = E, r = b, s = U;
  $e(f(this, H)), oe(f(this, H)), Et(f(this, H).ctx);
  try {
    return Ve.ensure(), e();
  } catch (i) {
    return as(i), null;
  } finally {
    $e(n), oe(r), Et(s);
  }
}, /**
 * Updates the pending count associated with the currently visible pending snippet,
 * if any, such that we can replace the snippet with content once work is done
 * @param {1 | -1} d
 * @param {Batch} batch
 */
jn = function(e, n) {
  var r;
  if (!this.has_pending_snippet()) {
    this.parent && k(r = this.parent, A, jn).call(r, e, n);
    return;
  }
  y(this, De, f(this, De) + e), f(this, De) === 0 && (k(this, A, Zt).call(this, n), f(this, V) && Pt(f(this, V), () => {
    y(this, V, null);
  }), f(this, xe) && (f(this, W).before(f(this, xe)), y(this, xe, null)));
}, /**
 * @param {unknown} error
 */
Yn = function(e) {
  f(this, ee) && (z(f(this, ee)), y(this, ee, null)), f(this, V) && (z(f(this, V)), y(this, V, null)), f(this, J) && (z(f(this, J)), y(this, J, null)), B && (Te(
    /** @type {TemplateNode} */
    f(this, It)
  ), Vi(), Te(ts()));
  var n = f(this, Z).onerror;
  let r = f(this, Z).failed;
  var s = !1, i = !1;
  const o = () => {
    if (s) {
      Ui();
      return;
    }
    s = !0, i && Mi(), f(this, J) !== null && Pt(f(this, J), () => {
      y(this, J, null);
    }), k(this, A, en).call(this, () => {
      k(this, A, Hn).call(this);
    });
  }, a = (l) => {
    try {
      i = !0, n == null || n(l, o), i = !1;
    } catch (c) {
      Me(c, f(this, H) && f(this, H).parent);
    }
    r && y(this, J, k(this, A, en).call(this, () => {
      try {
        return Re(() => {
          var c = (
            /** @type {Effect} */
            E
          );
          c.b = this, c.f |= Un, r(
            f(this, W),
            () => l,
            () => o
          );
        });
      } catch (c) {
        return Me(
          c,
          /** @type {Effect} */
          f(this, H).parent
        ), null;
      }
    }));
  };
  ut(() => {
    var l;
    try {
      l = this.transform_error(e);
    } catch (c) {
      Me(c, f(this, H) && f(this, H).parent);
      return;
    }
    l !== null && typeof l == "object" && typeof /** @type {any} */
    l.then == "function" ? l.then(
      a,
      /** @param {unknown} e */
      (c) => Me(c, f(this, H) && f(this, H).parent)
    ) : a(l);
  });
};
function Zi(t, e, n, r) {
  const s = fr;
  var i = t.filter((d) => !d.settled), o = e.map(s);
  if (n.length === 0 && i.length === 0) {
    r(o);
    return;
  }
  var a = (
    /** @type {Effect} */
    E
  ), l = eo(), c = i.length === 1 ? i[0].promise : i.length > 1 ? Promise.all(i.map((d) => d.promise)) : null;
  function h(d) {
    if ((a.f & ie) === 0) {
      l();
      try {
        r([...o, ...d]);
      } catch (_) {
        Me(_, a);
      }
      vn();
    }
  }
  var p = ds();
  if (n.length === 0) {
    c.then(() => h([])).finally(p);
    return;
  }
  function u() {
    Promise.all(n.map((d) => /* @__PURE__ */ to(d))).then(h).catch((d) => Me(d, a)).finally(p);
  }
  c ? c.then(() => {
    l(), u(), vn();
  }) : u();
}
function eo() {
  var t = (
    /** @type {Effect} */
    E
  ), e = b, n = U, r = (
    /** @type {Batch} */
    m
  );
  return function(i = !0) {
    $e(t), oe(e), Et(n), i && (t.f & ie) === 0 && (r == null || r.activate(), r == null || r.apply());
  };
}
function vn(t = !0) {
  $e(null), oe(null), Et(null), t && (m == null || m.deactivate());
}
function ds() {
  var t = (
    /** @type {Effect} */
    E
  ), e = t.b, n = (
    /** @type {Batch} */
    m
  ), r = !!(e != null && e.is_rendered());
  return e == null || e.update_pending_count(1, n), n.increment(r, t), () => {
    e == null || e.update_pending_count(-1, n), n.decrement(r, t);
  };
}
// @__NO_SIDE_EFFECTS__
function fr(t) {
  var e = M | q;
  return E !== null && (E.f |= st), {
    ctx: U,
    deps: null,
    effects: null,
    equals: ns,
    f: e,
    fn: t,
    reactions: null,
    rv: 0,
    v: (
      /** @type {V} */
      D
    ),
    wv: 0,
    parent: E,
    ac: null
  };
}
const Ot = Symbol("obsolete");
// @__NO_SIDE_EFFECTS__
function to(t, e, n) {
  let r = (
    /** @type {Effect | null} */
    E
  );
  r === null && xi();
  var s = (
    /** @type {Promise<V>} */
    /** @type {unknown} */
    void 0
  ), i = Yt(
    /** @type {V} */
    D
  ), o = !b, a = /* @__PURE__ */ new Set();
  return wo(() => {
    var d, _;
    var l = (
      /** @type {Effect} */
      E
    ), c = Gr();
    s = c.promise;
    try {
      Promise.resolve(t()).then(c.resolve, (v) => {
        v !== jt && c.reject(v);
      }).finally(vn);
    } catch (v) {
      c.reject(v), vn();
    }
    var h = (
      /** @type {Batch} */
      m
    );
    if (o) {
      if ((l.f & rt) !== 0)
        var p = ds();
      if (
        // boundary can be null if the async derived is inside an $effect.root not connected to the component render tree
        (d = r.b) != null && d.is_rendered()
      )
        (_ = h.async_deriveds.get(l)) == null || _.reject(Ot);
      else
        for (const v of a.values())
          v.reject(Ot);
      a.add(c), h.async_deriveds.set(l, c);
    }
    const u = (v, S = void 0) => {
      p == null || p(), a.delete(c), S !== Ot && (h.activate(), S ? (i.f |= Fe, gn(i, S)) : ((i.f & Fe) !== 0 && (i.f ^= Fe), gn(i, v)), h.deactivate());
    };
    c.promise.then(u, (v) => u(null, v || "unknown"));
  }), _o(() => {
    for (const l of a)
      l.reject(Ot);
  }), new Promise((l) => {
    function c(h) {
      function p() {
        h === s ? l(i) : c(s);
      }
      h.then(p, p);
    }
    c(s);
  });
}
// @__NO_SIDE_EFFECTS__
function no(t) {
  const e = /* @__PURE__ */ fr(t);
  return qs(e), e;
}
function ro(t) {
  var e = t.effects;
  if (e !== null) {
    t.effects = null;
    for (var n = 0; n < e.length; n += 1)
      z(
        /** @type {Effect} */
        e[n]
      );
  }
}
function lr(t) {
  var e, n = E, r = t.parent;
  if (!qe && r !== null && t.v !== D && // if it was never evaluated before, it's guaranteed to fail downstream, so we try to execute instead
  (r.f & (ie | se)) !== 0)
    return Fi(), t.v;
  $e(r);
  try {
    t.f &= ~tt, ro(t), e = Ms(t);
  } finally {
    $e(n);
  }
  return e;
}
function ps(t) {
  var e = lr(t);
  if (!t.equals(e) && (t.wv = Ds(), (!(m != null && m.is_fork) || t.deps === null) && (m !== null ? (m.capture(t, e, !0), Bt == null || Bt.capture(t, e, !0)) : t.v = e, t.deps === null))) {
    C(t, I);
    return;
  }
  qe || (F !== null ? (dr() || m != null && m.is_fork) && F.set(t, e) : ar(t));
}
function so(t) {
  var e;
  if (t.effects !== null)
    for (const n of t.effects)
      (n.teardown || n.ac) && ((e = n.teardown) == null || e.call(n), n.ac !== null && Sn(() => {
        n.ac.abort(jt), n.ac = null;
      }), n.fn !== null && (n.teardown = Ti), Dt(n, 0), pr(n));
}
function _s(t) {
  if (t.effects !== null)
    for (const e of t.effects)
      e.teardown && e.fn !== null && kt(e);
}
let Ln = null, it = null, m = null, Bt = null, F = null, zn = null, Lt = !1, qn = !1, ft = null, tn = null;
var Rr = 0;
let io = 1;
var dt, Ie, Xe, pt, _t, vt, Ne, yt, j, Ut, Be, fe, me, gt, Qe, $, Kn, Rt, Wn, vs, ys, ot, oo, xt;
const bn = class bn {
  constructor() {
    w(this, $);
    O(this, "id", io++);
    /** True as soon as `#process` was called */
    w(this, dt, !1);
    O(this, "linked", !0);
    /** @type {Batch | null} */
    w(this, Ie, null);
    /** @type {Batch | null} */
    w(this, Xe, null);
    /** @type {Map<Effect, ReturnType<typeof deferred<any>>>} */
    O(this, "async_deriveds", /* @__PURE__ */ new Map());
    /**
     * The current values of any signals that are updated in this batch.
     * Tuple format: [value, is_derived] (note: is_derived is false for deriveds, too, if they were overridden via assignment)
     * They keys of this map are identical to `this.#previous`
     * @type {Map<Value, [any, boolean]>}
     */
    O(this, "current", /* @__PURE__ */ new Map());
    /**
     * The values of any signals (sources and deriveds) that are updated in this batch _before_ those updates took place.
     * They keys of this map are identical to `this.#current`
     * @type {Map<Value, any>}
     */
    O(this, "previous", /* @__PURE__ */ new Map());
    /**
     * When the batch is committed (and the DOM is updated), we need to remove old branches
     * and append new ones by calling the functions added inside (if/each/key/etc) blocks
     * @type {Set<(batch: Batch) => void>}
     */
    w(this, pt, /* @__PURE__ */ new Set());
    /**
     * If a fork is discarded, we need to destroy any effects that are no longer needed
     * @type {Set<(batch: Batch) => void>}
     */
    w(this, _t, /* @__PURE__ */ new Set());
    /**
     * The number of async effects that are currently in flight
     */
    w(this, vt, 0);
    /**
     * Async effects that are currently in flight, _not_ inside a pending boundary
     * @type {Map<Effect, number>}
     */
    w(this, Ne, /* @__PURE__ */ new Map());
    /**
     * A deferred that resolves when the batch is committed, used with `settled()`
     * TODO replace with Promise.withResolvers once supported widely enough
     * @type {{ promise: Promise<void>, resolve: (value?: any) => void, reject: (reason: unknown) => void } | null}
     */
    w(this, yt, null);
    /**
     * The root effects that need to be flushed
     * @type {Effect[]}
     */
    w(this, j, []);
    /**
     * Effects created while this batch was active.
     * @type {Effect[]}
     */
    w(this, Ut, []);
    /**
     * Deferred effects (which run after async work has completed) that are DIRTY
     * @type {Set<Effect>}
     */
    w(this, Be, /* @__PURE__ */ new Set());
    /**
     * Deferred effects that are MAYBE_DIRTY
     * @type {Set<Effect>}
     */
    w(this, fe, /* @__PURE__ */ new Set());
    /**
     * A map of branches that still exist, but will be destroyed when this batch
     * is committed — we skip over these during `process`.
     * The value contains child effects that were dirty/maybe_dirty before being reset,
     * so they can be rescheduled if the branch survives.
     * @type {Map<Effect, { d: Effect[], m: Effect[] }>}
     */
    w(this, me, /* @__PURE__ */ new Map());
    /**
     * Inverse of #skipped_branches which we need to tell prior batches to unskip them when committing
     * @type {Set<Effect>}
     */
    w(this, gt, /* @__PURE__ */ new Set());
    O(this, "is_fork", !1);
    w(this, Qe, !1);
    it === null ? Ln = it = this : (y(it, Xe, this), y(this, Ie, it)), it = this;
  }
  /**
   * Add an effect to the #skipped_branches map and reset its children
   * @param {Effect} effect
   */
  skip_effect(e) {
    f(this, me).has(e) || f(this, me).set(e, { d: [], m: [] }), f(this, gt).delete(e);
  }
  /**
   * Remove an effect from the #skipped_branches map and reschedule
   * any tracked dirty/maybe_dirty child effects
   * @param {Effect} effect
   * @param {(e: Effect) => void} callback
   */
  unskip_effect(e, n = (r) => this.schedule(r)) {
    var r = f(this, me).get(e);
    if (r) {
      f(this, me).delete(e);
      for (var s of r.d)
        C(s, q), n(s);
      for (s of r.m)
        C(s, de), n(s);
    }
    f(this, gt).add(e);
  }
  /**
   * Associate a change to a given source with the current
   * batch, noting its previous and current values
   * @param {Value} source
   * @param {any} value
   * @param {boolean} [is_derived]
   */
  capture(e, n, r = !1) {
    e.v !== D && !this.previous.has(e) && this.previous.set(e, e.v), (e.f & Fe) === 0 && (this.current.set(e, [n, r]), F == null || F.set(e, n)), this.is_fork || (e.v = n);
  }
  activate() {
    m = this;
  }
  deactivate() {
    m = null, F = null;
  }
  flush() {
    try {
      qn = !0, m = this, k(this, $, Rt).call(this);
    } finally {
      Rr = 0, zn = null, ft = null, tn = null, qn = !1, m = null, F = null, Ze.clear();
    }
  }
  discard() {
    var e;
    for (const n of f(this, _t)) n(this);
    f(this, _t).clear();
    for (const n of this.async_deriveds.values())
      n.reject(Ot);
    k(this, $, xt).call(this), (e = f(this, yt)) == null || e.resolve();
  }
  /**
   * @param {Effect} effect
   */
  register_created_effect(e) {
    f(this, Ut).push(e);
  }
  /**
   * @param {boolean} blocking
   * @param {Effect} effect
   */
  increment(e, n) {
    if (y(this, vt, f(this, vt) + 1), e) {
      let r = f(this, Ne).get(n) ?? 0;
      f(this, Ne).set(n, r + 1);
    }
  }
  /**
   * @param {boolean} blocking
   * @param {Effect} effect
   */
  decrement(e, n) {
    if (y(this, vt, f(this, vt) - 1), e) {
      let r = f(this, Ne).get(n) ?? 0;
      r === 1 ? f(this, Ne).delete(n) : f(this, Ne).set(n, r - 1);
    }
    f(this, Qe) || (y(this, Qe, !0), ut(() => {
      y(this, Qe, !1), this.linked && this.flush();
    }));
  }
  /**
   * @param {Set<Effect>} dirty_effects
   * @param {Set<Effect>} maybe_dirty_effects
   */
  transfer_effects(e, n) {
    for (const r of e)
      f(this, Be).add(r);
    for (const r of n)
      f(this, fe).add(r);
    e.clear(), n.clear();
  }
  /** @param {(batch: Batch) => void} fn */
  oncommit(e) {
    f(this, pt).add(e);
  }
  /** @param {(batch: Batch) => void} fn */
  ondiscard(e) {
    f(this, _t).add(e);
  }
  settled() {
    return (f(this, yt) ?? y(this, yt, Gr())).promise;
  }
  static ensure() {
    if (m === null) {
      const e = m = new bn();
      !qn && !Lt && ut(() => {
        f(e, dt) || e.flush();
      });
    }
    return m;
  }
  apply() {
    {
      F = null;
      return;
    }
  }
  /**
   *
   * @param {Effect} effect
   */
  schedule(e) {
    var s;
    if (zn = e, (s = e.b) != null && s.is_pending && (e.f & (wt | kn | Zr)) !== 0 && (e.f & rt) === 0) {
      e.b.defer_effect(e);
      return;
    }
    for (var n = e; n.parent !== null; ) {
      n = n.parent;
      var r = n.f;
      if (ft !== null && n === E && (b === null || (b.f & M) === 0))
        return;
      if ((r & (ke | pe)) !== 0) {
        if ((r & I) === 0)
          return;
        n.f ^= I;
      }
    }
    f(this, j).push(n);
  }
};
dt = new WeakMap(), Ie = new WeakMap(), Xe = new WeakMap(), pt = new WeakMap(), _t = new WeakMap(), vt = new WeakMap(), Ne = new WeakMap(), yt = new WeakMap(), j = new WeakMap(), Ut = new WeakMap(), Be = new WeakMap(), fe = new WeakMap(), me = new WeakMap(), gt = new WeakMap(), Qe = new WeakMap(), $ = new WeakSet(), Kn = function() {
  if (this.is_fork) return !0;
  for (const r of f(this, Ne).keys()) {
    for (var e = r, n = !1; e.parent !== null; ) {
      if (f(this, me).has(e)) {
        n = !0;
        break;
      }
      e = e.parent;
    }
    if (!n)
      return !0;
  }
  return !1;
}, Rt = function() {
  var l, c, h, p;
  y(this, dt, !0), Rr++ > 1e3 && (k(this, $, xt).call(this), ao());
  for (const u of f(this, Be))
    f(this, fe).delete(u), C(u, q), this.schedule(u);
  for (const u of f(this, fe))
    C(u, de), this.schedule(u);
  const e = f(this, j);
  y(this, j, []), this.apply();
  var n = ft = [], r = [], s = tn = [];
  for (const u of e)
    try {
      k(this, $, Wn).call(this, u, n, r);
    } catch (d) {
      throw ws(u), k(this, $, Kn).call(this) || this.discard(), d;
    }
  if (m = null, s.length > 0) {
    var i = bn.ensure();
    for (const u of s)
      i.schedule(u);
  }
  if (ft = null, tn = null, k(this, $, Kn).call(this)) {
    k(this, $, ot).call(this, r), k(this, $, ot).call(this, n);
    for (const [u, d] of f(this, me))
      ms(u, d);
    s.length > 0 && /** @type {unknown} */
    k(l = m, $, Rt).call(l);
    return;
  }
  const o = k(this, $, vs).call(this);
  if (o) {
    k(this, $, ot).call(this, r), k(this, $, ot).call(this, n), k(c = o, $, ys).call(c, this);
    return;
  }
  f(this, Be).clear(), f(this, fe).clear();
  for (const u of f(this, pt)) u(this);
  f(this, pt).clear(), Bt = this, xr(r), xr(n), Bt = null, (h = f(this, yt)) == null || h.resolve();
  var a = (
    /** @type {Batch | null} */
    /** @type {unknown} */
    m
  );
  if (f(this, vt) === 0 && (f(this, j).length === 0 || a !== null) && k(this, $, xt).call(this), f(this, j).length > 0)
    if (a !== null) {
      const u = a;
      f(u, j).push(...f(this, j).filter((d) => !f(u, j).includes(d)));
    } else
      a = this;
  a !== null && k(p = a, $, Rt).call(p);
}, /**
 * Traverse the effect tree, executing effects or stashing
 * them for later execution as appropriate
 * @param {Effect} root
 * @param {Effect[]} effects
 * @param {Effect[]} render_effects
 */
Wn = function(e, n, r) {
  e.f ^= I;
  for (var s = e.first; s !== null; ) {
    var i = s.f, o = (i & (pe | ke)) !== 0, a = o && (i & I) !== 0, l = a || (i & se) !== 0 || f(this, me).has(s);
    if (!l && s.fn !== null) {
      o ? s.f ^= I : (i & wt) !== 0 ? n.push(s) : zt(s) && ((i & ue) !== 0 && f(this, fe).add(s), kt(s));
      var c = s.first;
      if (c !== null) {
        s = c;
        continue;
      }
    }
    for (; s !== null; ) {
      var h = s.next;
      if (h !== null) {
        s = h;
        break;
      }
      s = s.parent;
    }
  }
}, vs = function() {
  for (var e = f(this, Ie); e !== null; ) {
    if (!e.is_fork) {
      for (const [n, [, r]] of this.current)
        if (e.current.has(n) && !r)
          return e;
    }
    e = f(e, Ie);
  }
  return null;
}, /**
 * @param {Batch} batch
 */
ys = function(e) {
  var r;
  for (const [s, i] of e.current)
    !this.previous.has(s) && e.previous.has(s) && this.previous.set(s, e.previous.get(s)), this.current.set(s, i);
  for (const [s, i] of e.async_deriveds) {
    const o = this.async_deriveds.get(s);
    o && i.promise.then(o.resolve).catch(o.reject);
  }
  e.async_deriveds.clear(), this.transfer_effects(f(e, Be), f(e, fe));
  const n = (s) => {
    var i = s.reactions;
    if (i !== null && !((s.f & M) !== 0 && (s.f & (q | de)) === 0))
      for (const l of i) {
        var o = l.f;
        if ((o & M) !== 0)
          n(
            /** @type {Derived} */
            l
          );
        else {
          var a = (
            /** @type {Effect} */
            l
          );
          o & (ct | ue) && !this.async_deriveds.has(a) && (f(this, fe).delete(a), C(a, q), this.schedule(a));
        }
      }
  };
  for (const s of this.current.keys())
    n(s);
  this.oncommit(() => e.discard()), k(r = e, $, xt).call(r), m = this, k(this, $, Rt).call(this);
}, /**
 * @param {Effect[]} effects
 */
ot = function(e) {
  for (var n = 0; n < e.length; n += 1)
    ls(e[n], f(this, Be), f(this, fe));
}, oo = function() {
  var p;
  for (let u = Ln; u !== null; u = f(u, Xe)) {
    var e = u.id < this.id, n = [];
    for (const [d, [_, v]] of this.current) {
      if (u.current.has(d)) {
        var r = (
          /** @type {[any, boolean]} */
          u.current.get(d)[0]
        );
        if (e && _ !== r)
          u.current.set(d, [_, v]);
        else
          continue;
      }
      n.push(d);
    }
    if (e)
      for (const [d, _] of this.async_deriveds) {
        const v = u.async_deriveds.get(d);
        v && _.promise.then(v.resolve).catch(v.reject);
      }
    var s = [...u.current.keys()].filter(
      (d) => !/** @type {[any, boolean]} */
      u.current.get(d)[1]
    );
    if (!(!f(u, dt) || s.length === 0)) {
      var i = s.filter((d) => !this.current.has(d));
      if (i.length === 0)
        e && u.discard();
      else if (n.length > 0) {
        if (e)
          for (const d of f(this, gt))
            u.unskip_effect(d, (_) => {
              var v;
              (_.f & (ue | ct)) !== 0 ? u.schedule(_) : k(v = u, $, ot).call(v, [_]);
            });
        u.activate();
        var o = /* @__PURE__ */ new Set(), a = /* @__PURE__ */ new Map();
        for (var l of n)
          gs(l, i, o, a);
        a = /* @__PURE__ */ new Map();
        var c = [...u.current].filter(([d, _]) => {
          const v = this.current.get(d);
          return v ? v[0] !== _[0] || v[1] !== _[1] : !0;
        }).map(([d]) => d);
        if (c.length > 0)
          for (const d of f(this, Ut))
            (d.f & (ie | se | pn)) === 0 && cr(d, c, a) && ((d.f & (ct | ue)) !== 0 ? (C(d, q), u.schedule(d)) : f(u, Be).add(d));
        if (f(u, j).length > 0 && !f(u, Qe)) {
          u.apply();
          for (var h of f(u, j))
            k(p = u, $, Wn).call(p, h, [], []);
          y(u, j, []);
        }
        u.deactivate();
      }
    }
  }
}, xt = function() {
  if (this.linked) {
    var e = f(this, Ie), n = f(this, Xe);
    e === null ? Ln = n : y(e, Xe, n), n === null ? it = e : y(n, Ie, e), this.linked = !1;
  }
};
let Ve = bn;
function nn(t) {
  var e = Lt;
  Lt = !0;
  try {
    for (var n; ; ) {
      if (Ki(), m === null)
        return (
          /** @type {T} */
          n
        );
      m.flush();
    }
  } finally {
    Lt = e;
  }
}
function ao() {
  try {
    Li();
  } catch (t) {
    Me(t, zn);
  }
}
let ae = null;
function xr(t) {
  var e = t.length;
  if (e !== 0) {
    for (var n = 0; n < e; ) {
      var r = t[n++];
      if ((r.f & (ie | se)) === 0 && zt(r) && (ae = /* @__PURE__ */ new Set(), kt(r), r.deps === null && r.first === null && r.nodes === null && r.teardown === null && r.ac === null && Cs(r), (ae == null ? void 0 : ae.size) > 0)) {
        Ze.clear();
        for (const s of ae) {
          if ((s.f & (ie | se)) !== 0) continue;
          const i = [s];
          let o = s.parent;
          for (; o !== null; )
            ae.has(o) && (ae.delete(o), i.push(o)), o = o.parent;
          for (let a = i.length - 1; a >= 0; a--) {
            const l = i[a];
            (l.f & (ie | se)) === 0 && kt(l);
          }
        }
        ae.clear();
      }
    }
    ae = null;
  }
}
function gs(t, e, n, r) {
  if (!n.has(t) && (n.add(t), t.reactions !== null))
    for (const s of t.reactions) {
      const i = s.f;
      (i & M) !== 0 ? gs(
        /** @type {Derived} */
        s,
        e,
        n,
        r
      ) : (i & (ct | ue)) !== 0 && (i & q) === 0 && cr(s, e, r) && (C(s, q), ur(
        /** @type {Effect} */
        s
      ));
    }
}
function cr(t, e, n) {
  const r = n.get(t);
  if (r !== void 0) return r;
  if (t.deps !== null)
    for (const s of t.deps) {
      if (un.call(e, s))
        return !0;
      if ((s.f & M) !== 0 && cr(
        /** @type {Derived} */
        s,
        e,
        n
      ))
        return n.set(
          /** @type {Derived} */
          s,
          !0
        ), !0;
    }
  return n.set(t, !1), !1;
}
function ur(t) {
  m.schedule(t);
}
function ms(t, e) {
  if (!((t.f & pe) !== 0 && (t.f & I) !== 0)) {
    (t.f & q) !== 0 ? e.d.push(t) : (t.f & de) !== 0 && e.m.push(t), C(t, I);
    for (var n = t.first; n !== null; )
      ms(n, e), n = n.next;
  }
}
function ws(t) {
  C(t, I);
  for (var e = t.first; e !== null; )
    ws(e), e = e.next;
}
let yn = /* @__PURE__ */ new Set();
const Ze = /* @__PURE__ */ new Map();
let bs = !1;
function Yt(t, e) {
  var n = {
    f: 0,
    // TODO ideally we could skip this altogether, but it causes type errors
    v: t,
    reactions: null,
    equals: ns,
    rv: 0,
    wv: 0
  };
  return n;
}
// @__NO_SIDE_EFFECTS__
function Q(t, e) {
  const n = Yt(t);
  return qs(n), n;
}
// @__NO_SIDE_EFFECTS__
function fo(t, e = !1, n = !0) {
  const r = Yt(t);
  return e || (r.equals = Yi), r;
}
function R(t, e, n = !1) {
  b !== null && // since we are untracking the function inside `$inspect.with` we need to add this check
  // to ensure we error if state is set inside an inspect effect
  (!he || (b.f & pn) !== 0) && is() && (b.f & (M | ue | ct | pn)) !== 0 && (be === null || !be.has(t)) && Ii();
  let r = n ? Ct(e) : e;
  return gn(t, r, tn);
}
function gn(t, e, n = null) {
  if (!t.equals(e)) {
    Ze.set(t, qe ? e : t.v);
    var r = Ve.ensure();
    if (r.capture(t, e), (t.f & M) !== 0) {
      const s = (
        /** @type {Derived} */
        t
      );
      (t.f & q) !== 0 && lr(s), F === null && ar(s);
    }
    t.wv = Ds(), Es(t, q, n), E !== null && (E.f & I) !== 0 && (E.f & (pe | ke)) === 0 && (G === null ? ko([t]) : G.push(t)), !r.is_fork && yn.size > 0 && !bs && lo();
  }
  return e;
}
function lo() {
  bs = !1;
  for (const t of yn) {
    (t.f & I) !== 0 && C(t, de);
    let e;
    try {
      e = zt(t);
    } catch {
      e = !0;
    }
    e && kt(t);
  }
  yn.clear();
}
function qt(t) {
  R(t, t.v + 1);
}
function Es(t, e, n) {
  var r = t.reactions;
  if (r !== null)
    for (var s = r.length, i = 0; i < s; i++) {
      var o = r[i], a = o.f, l = (a & q) === 0;
      if (l && C(o, e), (a & pn) !== 0)
        yn.add(
          /** @type {Effect} */
          o
        );
      else if ((a & M) !== 0) {
        var c = (
          /** @type {Derived} */
          o
        );
        F == null || F.delete(c), (a & tt) === 0 && (a & re && (E === null || (E.f & _n) === 0) && (o.f |= tt), Es(c, de, n));
      } else if (l) {
        var h = (
          /** @type {Effect} */
          o
        );
        (a & ue) !== 0 && ae !== null && ae.add(h), n !== null ? n.push(h) : ur(h);
      }
    }
}
function Ct(t) {
  if (typeof t != "object" || t === null || Bn in t)
    return t;
  const e = Qr(t);
  if (e !== Ei && e !== ki)
    return t;
  var n = /* @__PURE__ */ new Map(), r = gi(t), s = /* @__PURE__ */ Q(0), i = et, o = (a) => {
    if (et === i)
      return a();
    var l = b, c = et;
    oe(null), qr(i);
    var h = a();
    return oe(l), qr(c), h;
  };
  return r && n.set("length", /* @__PURE__ */ Q(
    /** @type {any[]} */
    t.length
  )), new Proxy(
    /** @type {any} */
    t,
    {
      defineProperty(a, l, c) {
        (!("value" in c) || c.configurable === !1 || c.enumerable === !1 || c.writable === !1) && Pi();
        var h = n.get(l);
        return h === void 0 ? o(() => {
          var p = /* @__PURE__ */ Q(c.value);
          return n.set(l, p), p;
        }) : R(h, c.value, !0), !0;
      },
      deleteProperty(a, l) {
        var c = n.get(l);
        if (c === void 0) {
          if (l in a) {
            const h = o(() => /* @__PURE__ */ Q(D));
            n.set(l, h), qt(s);
          }
        } else
          R(c, D), qt(s);
        return !0;
      },
      get(a, l, c) {
        var d;
        if (l === Bn)
          return t;
        var h = n.get(l), p = l in a;
        if (h === void 0 && (!p || (d = lt(a, l)) != null && d.writable) && (h = o(() => {
          var _ = Ct(p ? a[l] : D), v = /* @__PURE__ */ Q(_);
          return v;
        }), n.set(l, h)), h !== void 0) {
          var u = x(h);
          return u === D ? void 0 : u;
        }
        return Reflect.get(a, l, c);
      },
      getOwnPropertyDescriptor(a, l) {
        var c = Reflect.getOwnPropertyDescriptor(a, l);
        if (c && "value" in c) {
          var h = n.get(l);
          h && (c.value = x(h));
        } else if (c === void 0) {
          var p = n.get(l), u = p == null ? void 0 : p.v;
          if (p !== void 0 && u !== D)
            return {
              enumerable: !0,
              configurable: !0,
              value: u,
              writable: !0
            };
        }
        return c;
      },
      has(a, l) {
        var u;
        if (l === Bn)
          return !0;
        var c = n.get(l), h = c !== void 0 && c.v !== D || Reflect.has(a, l);
        if (c !== void 0 || E !== null && (!h || (u = lt(a, l)) != null && u.writable)) {
          c === void 0 && (c = o(() => {
            var d = h ? Ct(a[l]) : D, _ = /* @__PURE__ */ Q(d);
            return _;
          }), n.set(l, c));
          var p = x(c);
          if (p === D)
            return !1;
        }
        return h;
      },
      set(a, l, c, h) {
        var ve;
        var p = n.get(l), u = l in a;
        if (r && l === "length")
          for (var d = c; d < /** @type {Source<number>} */
          p.v; d += 1) {
            var _ = n.get(d + "");
            _ !== void 0 ? R(_, D) : d in a && (_ = o(() => /* @__PURE__ */ Q(D)), n.set(d + "", _));
          }
        if (p === void 0)
          (!u || (ve = lt(a, l)) != null && ve.writable) && (p = o(() => /* @__PURE__ */ Q(void 0)), R(p, Ct(c)), n.set(l, p));
        else {
          u = p.v !== D;
          var v = o(() => Ct(c));
          R(p, v);
        }
        var S = Reflect.getOwnPropertyDescriptor(a, l);
        if (S != null && S.set && S.set.call(h, c), !u) {
          if (r && typeof l == "string") {
            var L = (
              /** @type {Source<number>} */
              n.get("length")
            ), _e = Number(l);
            Number.isInteger(_e) && _e >= L.v && R(L, _e + 1);
          }
          qt(s);
        }
        return !0;
      },
      ownKeys(a) {
        x(s);
        var l = Reflect.ownKeys(a).filter((p) => {
          var u = n.get(p);
          return u === void 0 || u.v !== D;
        });
        for (var [c, h] of n)
          h.v !== D && !(c in a) && l.push(c);
        return l;
      },
      setPrototypeOf() {
        Di();
      }
    }
  );
}
var Cr, ks, Ts, $s;
function Jn() {
  if (Cr === void 0) {
    Cr = window, ks = /Firefox/.test(navigator.userAgent);
    var t = Element.prototype, e = Node.prototype, n = Text.prototype;
    Ts = lt(e, "firstChild").get, $s = lt(e, "nextSibling").get, Ar(t) && (t[Vn] = void 0, t[Gt] = null, t[Ri] = void 0, t.__e = void 0), Ar(n) && (n[At] = void 0);
  }
}
function nt(t = "") {
  return document.createTextNode(t);
}
// @__NO_SIDE_EFFECTS__
function mn(t) {
  return (
    /** @type {TemplateNode | null} */
    Ts.call(t)
  );
}
// @__NO_SIDE_EFFECTS__
function He(t) {
  return (
    /** @type {TemplateNode | null} */
    $s.call(t)
  );
}
function ge(t, e) {
  if (!B)
    return /* @__PURE__ */ mn(t);
  var n = /* @__PURE__ */ mn(N);
  if (n === null)
    n = N.appendChild(nt());
  else if (e && n.nodeType !== ir) {
    var r = nt();
    return n == null || n.before(r), Te(r), r;
  }
  return e && Ss(
    /** @type {Text} */
    n
  ), Te(n), n;
}
function Pe(t, e = 1, n = !1) {
  let r = B ? N : t;
  for (var s; e--; )
    s = r, r = /** @type {TemplateNode} */
    /* @__PURE__ */ He(r);
  if (!B)
    return r;
  if (n) {
    if ((r == null ? void 0 : r.nodeType) !== ir) {
      var i = nt();
      return r === null ? s == null || s.after(i) : r.before(i), Te(i), i;
    }
    Ss(
      /** @type {Text} */
      r
    );
  }
  return Te(r), r;
}
function co(t) {
  t.textContent = "";
}
function uo() {
  return !1;
}
function hr(t, e, n) {
  return (
    /** @type {T extends keyof HTMLElementTagNameMap ? HTMLElementTagNameMap[T] : Element} */
    document.createElement(t)
  );
}
function Ss(t) {
  if (
    /** @type {string} */
    t.nodeValue.length < 65536
  )
    return;
  let e = t.nextSibling;
  for (; e !== null && e.nodeType === ir; )
    e.remove(), t.nodeValue += /** @type {string} */
    e.nodeValue, e = t.nextSibling;
}
function ho(t) {
  E === null && (b === null && Bi(), Ni()), qe && Ci();
}
function po(t, e) {
  var n = e.last;
  n === null ? e.last = e.first = t : (n.next = t, t.prev = n, e.last = t);
}
function Ae(t, e) {
  var n = E;
  n !== null && (n.f & se) !== 0 && (t |= se);
  var r = {
    ctx: U,
    deps: null,
    nodes: null,
    f: t | q | re,
    first: null,
    fn: e,
    last: null,
    next: null,
    parent: n,
    b: n && n.b,
    prev: null,
    teardown: null,
    wv: 0,
    ac: null
  };
  m == null || m.register_created_effect(r);
  var s = r;
  if ((t & wt) !== 0)
    ft !== null ? ft.push(r) : Ve.ensure().schedule(r);
  else if (e !== null) {
    try {
      kt(r);
    } catch (o) {
      throw z(r), o;
    }
    s.deps === null && s.teardown === null && s.nodes === null && s.first === s.last && // either `null`, or a singular child
    (s.f & st) === 0 && (s = s.first, (t & ue) !== 0 && (t & bt) !== 0 && s !== null && (s.f |= bt));
  }
  if (s !== null && (s.parent = n, n !== null && po(s, n), b !== null && (b.f & M) !== 0 && (t & ke) === 0)) {
    var i = (
      /** @type {Derived} */
      b
    );
    (i.effects ?? (i.effects = [])).push(s);
  }
  return r;
}
function dr() {
  return b !== null && !he;
}
function _o(t) {
  const e = Ae(kn, null);
  return C(e, I), e.teardown = t, e;
}
function vo(t) {
  ho();
  var e = (
    /** @type {Effect} */
    E.f
  ), n = !b && (e & pe) !== 0 && U !== null && !U.i;
  if (n) {
    var r = (
      /** @type {ComponentContext} */
      U
    );
    (r.e ?? (r.e = [])).push(t);
  } else
    return As(t);
}
function As(t) {
  return Ae(wt | Ai, t);
}
function yo(t) {
  Ve.ensure();
  const e = Ae(ke | st, t);
  return () => {
    z(e);
  };
}
function go(t) {
  Ve.ensure();
  const e = Ae(ke | st, t);
  return (n = {}) => new Promise((r) => {
    n.outro ? Pt(e, () => {
      z(e), r(void 0);
    }) : (z(e), r(void 0));
  });
}
function mo(t) {
  return Ae(wt, t);
}
function wo(t) {
  return Ae(ct | st, t);
}
function Os(t, e = 0) {
  return Ae(kn | e, t);
}
function Nr(t, e = [], n = [], r = []) {
  Zi(r, e, n, (s) => {
    Ae(kn, () => {
      t(...s.map(x));
    });
  });
}
function Rs(t, e = 0) {
  var n = Ae(ue | e, t);
  return n;
}
function Re(t) {
  return Ae(pe | st, t);
}
function xs(t) {
  var e = t.teardown;
  if (e !== null) {
    const n = qe, r = b;
    Lr(!0), oe(null);
    try {
      e.call(null);
    } finally {
      Lr(n), oe(r);
    }
  }
}
function pr(t, e = !1) {
  var n = t.first;
  for (t.first = t.last = null; n !== null; ) {
    const s = n.ac;
    s !== null && Sn(() => {
      s.abort(jt);
    });
    var r = n.next;
    (n.f & ke) !== 0 ? n.parent = null : z(n, e), n = r;
  }
}
function bo(t) {
  for (var e = t.first; e !== null; ) {
    var n = e.next;
    (e.f & pe) === 0 && z(e), e = n;
  }
}
function z(t, e = !0) {
  var n = !1;
  (e || (t.f & Si) !== 0) && t.nodes !== null && t.nodes.end !== null && (Eo(
    t.nodes.start,
    /** @type {TemplateNode} */
    t.nodes.end
  ), n = !0), t.f |= Or, pr(t, e && !n), Dt(t, 0);
  var r = t.nodes && t.nodes.t;
  if (r !== null)
    for (const i of r)
      i.stop();
  xs(t), t.f ^= Or, t.f |= ie;
  var s = t.parent;
  s !== null && s.first !== null && Cs(t), t.next = t.prev = t.teardown = t.ctx = t.deps = t.fn = t.nodes = t.ac = t.b = null;
}
function Eo(t, e) {
  for (; t !== null; ) {
    var n = t === e ? null : /* @__PURE__ */ He(t);
    t.remove(), t = n;
  }
}
function Cs(t) {
  var e = t.parent, n = t.prev, r = t.next;
  n !== null && (n.next = r), r !== null && (r.prev = n), e !== null && (e.first === t && (e.first = r), e.last === t && (e.last = n));
}
function Pt(t, e, n = !0) {
  var r = [];
  Ns(t, r, !0);
  var s = () => {
    n && z(t), e && e();
  }, i = r.length;
  if (i > 0) {
    var o = () => --i || s();
    for (var a of r)
      a.out(o);
  } else
    s();
}
function Ns(t, e, n) {
  if ((t.f & se) === 0) {
    t.f ^= se;
    var r = t.nodes && t.nodes.t;
    if (r !== null)
      for (const a of r)
        (a.is_global || n) && e.push(a);
    for (var s = t.first; s !== null; ) {
      var i = s.next;
      if ((s.f & ke) === 0) {
        var o = (s.f & bt) !== 0 || // If this is a branch effect without a block effect parent,
        // it means the parent block effect was pruned. In that case,
        // transparency information was transferred to the branch effect.
        (s.f & pe) !== 0 && (t.f & ue) !== 0;
        Ns(s, e, o ? n : !1);
      }
      s = i;
    }
  }
}
function Br(t) {
  Bs(t, !0);
}
function Bs(t, e) {
  if ((t.f & se) !== 0) {
    t.f ^= se, (t.f & I) === 0 && (C(t, q), Ve.ensure().schedule(t));
    for (var n = t.first; n !== null; ) {
      var r = n.next, s = (n.f & bt) !== 0 || (n.f & pe) !== 0;
      Bs(n, s ? e : !1), n = r;
    }
    var i = t.nodes && t.nodes.t;
    if (i !== null)
      for (const o of i)
        (o.is_global || e) && o.in();
  }
}
function Ls(t, e) {
  if (t.nodes)
    for (var n = t.nodes.start, r = t.nodes.end; n !== null; ) {
      var s = n === r ? null : /* @__PURE__ */ He(n);
      e.append(n), n = s;
    }
}
let rn = !1, qe = !1;
function Lr(t) {
  qe = t;
}
let b = null, he = !1;
function oe(t) {
  b = t;
}
let E = null;
function $e(t) {
  E = t;
}
let be = null;
function qs(t) {
  b !== null && (be ?? (be = /* @__PURE__ */ new Set())).add(t);
}
let Y = null, K = 0, G = null;
function ko(t) {
  G = t;
}
let Ps = 1, ze = 0, et = ze;
function qr(t) {
  et = t;
}
function Ds() {
  return ++Ps;
}
function zt(t) {
  var e = t.f;
  if ((e & q) !== 0)
    return !0;
  if (e & M && (t.f &= ~tt), (e & de) !== 0) {
    for (var n = (
      /** @type {Value[]} */
      t.deps
    ), r = n.length, s = 0; s < r; s++) {
      var i = n[s];
      if (zt(
        /** @type {Derived} */
        i
      ) && ps(
        /** @type {Derived} */
        i
      ), i.wv > t.wv)
        return !0;
    }
    (e & re) !== 0 && // During time traveling we don't want to reset the status so that
    // traversal of the graph in the other batches still happens
    F === null && C(t, I);
  }
  return !1;
}
function Is(t, e, n = !0) {
  var r = t.reactions;
  if (r !== null && !(be !== null && be.has(t)))
    for (var s = 0; s < r.length; s++) {
      var i = r[s];
      (i.f & M) !== 0 ? Is(
        /** @type {Derived} */
        i,
        e,
        !1
      ) : e === i && (n ? C(i, q) : (i.f & I) !== 0 && C(i, de), ur(
        /** @type {Effect} */
        i
      ));
    }
}
function Ms(t) {
  var v;
  var e = Y, n = K, r = G, s = b, i = be, o = U, a = he, l = et, c = t.f;
  Y = /** @type {null | Value[]} */
  null, K = 0, G = null, b = (c & (pe | ke)) === 0 ? t : null, be = null, Et(t.ctx), he = !1, et = ++ze, t.ac !== null && (Sn(() => {
    t.ac.abort(jt);
  }), t.ac = null);
  try {
    t.f |= _n;
    var h = (
      /** @type {Function} */
      t.fn
    ), p = h();
    t.f |= rt;
    var u = t.deps, d = m == null ? void 0 : m.is_fork;
    if (Y !== null) {
      var _;
      if (d || Dt(t, K), u !== null && K > 0)
        for (u.length = K + Y.length, _ = 0; _ < Y.length; _++)
          u[K + _] = Y[_];
      else
        t.deps = u = Y;
      if (dr() && (t.f & re) !== 0)
        for (_ = K; _ < u.length; _++)
          ((v = u[_]).reactions ?? (v.reactions = [])).push(t);
    } else !d && u !== null && K < u.length && (Dt(t, K), u.length = K);
    if (is() && G !== null && !he && u !== null && (t.f & (M | de | q)) === 0)
      for (_ = 0; _ < /** @type {Source[]} */
      G.length; _++)
        Is(
          G[_],
          /** @type {Effect} */
          t
        );
    if (s !== null && s !== t) {
      if (ze++, s.deps !== null)
        for (let S = 0; S < n; S += 1)
          s.deps[S].rv = ze;
      if (e !== null)
        for (const S of e)
          S.rv = ze;
      G !== null && (r === null ? r = G : r.push(.../** @type {Source[]} */
      G));
    }
    return (t.f & Fe) !== 0 && (t.f ^= Fe), p;
  } catch (S) {
    return as(S);
  } finally {
    t.f ^= _n, Y = e, K = n, G = r, b = s, be = i, Et(o), he = a, et = l;
  }
}
function To(t, e) {
  let n = e.reactions;
  if (n !== null) {
    var r = mi.call(n, t);
    if (r !== -1) {
      var s = n.length - 1;
      s === 0 ? n = e.reactions = null : (n[r] = n[s], n.pop());
    }
  }
  if (n === null && (e.f & M) !== 0 && // Destroying a child effect while updating a parent effect can cause a dependency to appear
  // to be unused, when in fact it is used by the currently-updating parent. Checking `new_deps`
  // allows us to skip the expensive work of disconnecting and immediately reconnecting it
  (Y === null || !un.call(Y, e))) {
    var i = (
      /** @type {Derived} */
      e
    );
    (i.f & re) !== 0 && (i.f ^= re, i.f &= ~tt), i.v !== D && ar(i), i.ac !== null && Sn(() => {
      i.ac.abort(jt), i.ac = null, C(i, q);
    }), so(i), Dt(i, 0);
  }
}
function Dt(t, e) {
  var n = t.deps;
  if (n !== null)
    for (var r = e; r < n.length; r++)
      To(t, n[r]);
}
function kt(t) {
  var e = t.f;
  if ((e & ie) === 0) {
    C(t, I);
    var n = E, r = rn;
    E = t, rn = (e & (pe | ke)) === 0;
    try {
      (e & (ue | Zr)) !== 0 ? bo(t) : pr(t), xs(t);
      var s = Ms(t);
      t.teardown = typeof s == "function" ? s : null, t.wv = Ps;
      var i;
      Xr && zi && (t.f & q) !== 0 && t.deps;
    } finally {
      rn = r, E = n;
    }
  }
}
function x(t) {
  var e = t.f, n = (e & M) !== 0;
  if (b !== null && !he) {
    var r = E !== null && (E.f & ie) !== 0;
    if (!r && (be === null || !be.has(t))) {
      var s = b.deps;
      if ((b.f & _n) !== 0)
        t.rv < ze && (t.rv = ze, Y === null && s !== null && s[K] === t ? K++ : Y === null ? Y = [t] : Y.push(t));
      else {
        b.deps ?? (b.deps = []), un.call(b.deps, t) || b.deps.push(t);
        var i = t.reactions;
        i === null ? t.reactions = [b] : un.call(i, b) || i.push(b);
      }
    }
  }
  if (qe && Ze.has(t))
    return Ze.get(t);
  if (n) {
    var o = (
      /** @type {Derived} */
      t
    );
    if (qe) {
      var a = o.v;
      return ((o.f & I) === 0 && o.reactions !== null || Us(o)) && (a = lr(o)), Ze.set(o, a), a;
    }
    var l = (o.f & re) === 0 && !he && b !== null && (rn || (b.f & re) !== 0), c = (o.f & rt) === 0;
    zt(o) && (l && (o.f |= re), ps(o)), l && !c && (_s(o), Fs(o));
  }
  if (F != null && F.has(t))
    return F.get(t);
  if ((t.f & Fe) !== 0)
    throw t.v;
  return t.v;
}
function Fs(t) {
  if (t.f |= re, t.deps !== null)
    for (const e of t.deps)
      (e.reactions ?? (e.reactions = [])).push(t), (e.f & M) !== 0 && (e.f & re) === 0 && (_s(
        /** @type {Derived} */
        e
      ), Fs(
        /** @type {Derived} */
        e
      ));
}
function Us(t) {
  if (t.v === D) return !0;
  if (t.deps === null) return !1;
  for (const e of t.deps)
    if (Ze.has(e) || (e.f & M) !== 0 && Us(
      /** @type {Derived} */
      e
    ))
      return !0;
  return !1;
}
function _r(t) {
  var e = he;
  try {
    return he = !0, t();
  } finally {
    he = e;
  }
}
const Ke = Symbol("events"), Vs = /* @__PURE__ */ new Set(), Xn = /* @__PURE__ */ new Set();
function $o(t, e, n) {
  (e[Ke] ?? (e[Ke] = {}))[t] = n;
}
function So(t) {
  for (var e = 0; e < t.length; e++)
    Vs.add(t[e]);
  for (var n of Xn)
    n(t);
}
let Pr = null;
function Dr(t) {
  var v, S;
  var e = this, n = (
    /** @type {Node} */
    e.ownerDocument
  ), r = t.type, s = ((v = t.composedPath) == null ? void 0 : v.call(t)) || [], i = (
    /** @type {null | Element} */
    s[0] || t.target
  );
  Pr = t;
  var o = 0, a = Pr === t && t[Ke];
  if (a) {
    var l = s.indexOf(a);
    if (l !== -1 && (e === document || e === /** @type {any} */
    window)) {
      t[Ke] = e;
      return;
    }
    var c = s.indexOf(e);
    if (c === -1)
      return;
    l <= c && (o = l);
  }
  if (i = /** @type {Element} */
  s[o] || t.target, i !== e) {
    dn(t, "currentTarget", {
      configurable: !0,
      get() {
        return i || n;
      }
    });
    var h = b, p = E;
    oe(null), $e(null);
    try {
      for (var u, d = []; i !== null && i !== e; ) {
        try {
          var _ = (S = i[Ke]) == null ? void 0 : S[r];
          _ != null && (!/** @type {any} */
          i.disabled || // DOM could've been updated already by the time this is reached, so we check this as well
          // -> the target could not have been disabled because it emits the event in the first place
          t.target === i) && _.call(i, t);
        } catch (L) {
          u ? d.push(L) : u = L;
        }
        if (t.cancelBubble) break;
        o++, i = o < s.length ? (
          /** @type {Element} */
          s[o]
        ) : null;
      }
      if (u) {
        for (let L of d)
          queueMicrotask(() => {
            throw L;
          });
        throw u;
      }
    } finally {
      t[Ke] = e, delete t.currentTarget, oe(h), $e(p);
    }
  }
}
var zr;
const Pn = (
  // We gotta write it like this because after downleveling the pure comment may end up in the wrong location
  ((zr = globalThis == null ? void 0 : globalThis.window) == null ? void 0 : zr.trustedTypes) && /* @__PURE__ */ globalThis.window.trustedTypes.createPolicy("svelte-trusted-html", {
    /** @param {string} html */
    createHTML: (t) => t
  })
);
function Ao(t) {
  return (
    /** @type {string} */
    (Pn == null ? void 0 : Pn.createHTML(t)) ?? t
  );
}
function Oo(t) {
  var e = hr("template");
  return e.innerHTML = Ao(t.replaceAll("<!>", "<!---->")), e.content;
}
function Qn(t, e) {
  var n = (
    /** @type {Effect} */
    E
  );
  n.nodes === null && (n.nodes = { start: t, end: e, a: null, t: null });
}
// @__NO_SIDE_EFFECTS__
function Hs(t, e) {
  var n = (e & vi) !== 0, r, s = !t.startsWith("<!>");
  return () => {
    if (B)
      return Qn(N, null), N;
    r === void 0 && (r = Oo(s ? t : "<!>" + t), r = /** @type {TemplateNode} */
    /* @__PURE__ */ mn(r));
    var i = (
      /** @type {TemplateNode} */
      n || ks ? document.importNode(r, !0) : r.cloneNode(!0)
    );
    return Qn(i, i), i;
  };
}
function Gn(t, e) {
  if (B) {
    var n = (
      /** @type {Effect & { nodes: EffectNodes }} */
      E
    );
    ((n.f & rt) === 0 || n.nodes.end === null) && (n.nodes.end = N), or();
    return;
  }
  t !== null && t.before(
    /** @type {Node} */
    e
  );
}
const Ro = ["touchstart", "touchmove"];
function xo(t) {
  return Ro.includes(t);
}
function je(t, e) {
  var n = e == null ? "" : typeof e == "object" ? `${e}` : e;
  n !== /** @type {any} */
  (t[At] ?? (t[At] = t.nodeValue)) && (t[At] = n, t.nodeValue = `${n}`);
}
function js(t, e) {
  return Ys(t, e);
}
function Co(t, e) {
  Jn(), e.intro = e.intro ?? !1;
  const n = e.target, r = B, s = N;
  try {
    for (var i = /* @__PURE__ */ mn(n); i && (i.nodeType !== Tn || /** @type {Comment} */
    i.data !== Kr); )
      i = /* @__PURE__ */ He(i);
    if (!i)
      throw mt;
    at(!0), Te(
      /** @type {Comment} */
      i
    );
    const o = Ys(t, { ...e, anchor: i });
    return at(!1), /**  @type {Exports} */
    o;
  } catch (o) {
    if (o instanceof Error && o.message.split(`
`).some((a) => a.startsWith("https://svelte.dev/e/")))
      throw o;
    return o !== mt && console.warn("Failed to hydrate: ", o), e.recover === !1 && qi(), Jn(), co(n), at(!1), js(t, e);
  } finally {
    at(r), Te(s);
  }
}
const Jt = /* @__PURE__ */ new Map();
function Ys(t, { target: e, anchor: n, props: r = {}, events: s, context: i, intro: o = !0, transformError: a }) {
  Jn();
  var l = void 0, c = go(() => {
    var h = n ?? e.appendChild(nt());
    Qi(
      /** @type {TemplateNode} */
      h,
      {
        pending: () => {
        }
      },
      (d) => {
        rs({});
        var _ = (
          /** @type {ComponentContext} */
          U
        );
        if (i && (_.c = i), s && (r.$$events = s), B && Qn(
          /** @type {TemplateNode} */
          d,
          null
        ), l = t(d, r) || {}, B && (E.nodes.end = N, N === null || N.nodeType !== Tn || /** @type {Comment} */
        N.data !== Jr))
          throw $n(), mt;
        ss();
      },
      a
    );
    var p = /* @__PURE__ */ new Set(), u = (d) => {
      for (var _ = 0; _ < d.length; _++) {
        var v = d[_];
        if (!p.has(v)) {
          p.add(v);
          var S = xo(v);
          for (const ve of [e, document]) {
            var L = Jt.get(ve);
            L === void 0 && (L = /* @__PURE__ */ new Map(), Jt.set(ve, L));
            var _e = L.get(v);
            _e === void 0 ? (ve.addEventListener(v, Dr, { passive: S }), L.set(v, 1)) : L.set(v, _e + 1);
          }
        }
      }
    };
    return u(wi(Vs)), Xn.add(u), () => {
      var S;
      for (var d of p)
        for (const L of [e, document]) {
          var _ = (
            /** @type {Map<string, number>} */
            Jt.get(L)
          ), v = (
            /** @type {number} */
            _.get(d)
          );
          --v == 0 ? (L.removeEventListener(d, Dr), _.delete(d), _.size === 0 && Jt.delete(L)) : _.set(d, v);
        }
      Xn.delete(u), h !== n && ((S = h.parentNode) == null || S.removeChild(h));
    };
  });
  return Zn.set(l, c), l;
}
let Zn = /* @__PURE__ */ new WeakMap();
function No(t, e) {
  const n = Zn.get(t);
  return n ? (Zn.delete(t), n(e)) : Promise.resolve();
}
var le, we, X, Ge, Vt, Ht, En;
class Bo {
  /**
   * @param {TemplateNode} anchor
   * @param {boolean} transition
   */
  constructor(e, n = !0) {
    /** @type {TemplateNode} */
    O(this, "anchor");
    /** @type {Map<Batch, Key>} */
    w(this, le, /* @__PURE__ */ new Map());
    /**
     * Map of keys to effects that are currently rendered in the DOM.
     * These effects are visible and actively part of the document tree.
     * Example:
     * ```
     * {#if condition}
     * 	foo
     * {:else}
     * 	bar
     * {/if}
     * ```
     * Can result in the entries `true->Effect` and `false->Effect`
     * @type {Map<Key, Effect>}
     */
    w(this, we, /* @__PURE__ */ new Map());
    /**
     * Similar to #onscreen with respect to the keys, but contains branches that are not yet
     * in the DOM, because their insertion is deferred.
     * @type {Map<Key, Branch>}
     */
    w(this, X, /* @__PURE__ */ new Map());
    /**
     * Keys of effects that are currently outroing
     * @type {Set<Key>}
     */
    w(this, Ge, /* @__PURE__ */ new Set());
    /**
     * Whether to pause (i.e. outro) on change, or destroy immediately.
     * This is necessary for `<svelte:element>`
     */
    w(this, Vt, !0);
    /**
     * @param {Batch} batch
     */
    w(this, Ht, (e) => {
      if (f(this, le).has(e)) {
        var n = (
          /** @type {Key} */
          f(this, le).get(e)
        ), r = f(this, we).get(n);
        if (r)
          Br(r), f(this, Ge).delete(n);
        else {
          var s = f(this, X).get(n);
          s && (Br(s.effect), f(this, we).set(n, s.effect), f(this, X).delete(n), s.fragment.lastChild.remove(), this.anchor.before(s.fragment), r = s.effect);
        }
        for (const [i, o] of f(this, le)) {
          if (f(this, le).delete(i), i === e)
            break;
          const a = f(this, X).get(o);
          a && (z(a.effect), f(this, X).delete(o));
        }
        for (const [i, o] of f(this, we)) {
          if (i === n || f(this, Ge).has(i)) continue;
          const a = () => {
            if (Array.from(f(this, le).values()).includes(i)) {
              var c = document.createDocumentFragment();
              Ls(o, c), c.append(nt()), f(this, X).set(i, { effect: o, fragment: c });
            } else
              z(o);
            f(this, Ge).delete(i), f(this, we).delete(i);
          };
          f(this, Vt) || !r ? (f(this, Ge).add(i), Pt(o, a, !1)) : a();
        }
      }
    });
    /**
     * @param {Batch} batch
     */
    w(this, En, (e) => {
      f(this, le).delete(e);
      const n = Array.from(f(this, le).values());
      for (const [r, s] of f(this, X))
        n.includes(r) || (z(s.effect), f(this, X).delete(r));
    });
    this.anchor = e, y(this, Vt, n);
  }
  /**
   *
   * @param {any} key
   * @param {null | ((target: TemplateNode) => void)} fn
   */
  ensure(e, n) {
    var r = (
      /** @type {Batch} */
      m
    ), s = uo();
    if (n && !f(this, we).has(e) && !f(this, X).has(e))
      if (s) {
        var i = document.createDocumentFragment(), o = nt();
        i.append(o), f(this, X).set(e, {
          effect: Re(() => n(o)),
          fragment: i
        });
      } else
        f(this, we).set(
          e,
          Re(() => n(this.anchor))
        );
    if (f(this, le).set(r, e), s) {
      for (const [a, l] of f(this, we))
        a === e ? r.unskip_effect(l) : r.skip_effect(l);
      for (const [a, l] of f(this, X))
        a === e ? r.unskip_effect(l.effect) : r.skip_effect(l.effect);
      r.oncommit(f(this, Ht)), r.ondiscard(f(this, En));
    } else
      B && (this.anchor = N), f(this, Ht).call(this, r);
  }
}
le = new WeakMap(), we = new WeakMap(), X = new WeakMap(), Ge = new WeakMap(), Vt = new WeakMap(), Ht = new WeakMap(), En = new WeakMap();
function zs(t) {
  U === null && es(), vo(() => {
    const e = _r(t);
    if (typeof e == "function") return (
      /** @type {() => void} */
      e
    );
  });
}
function Lo(t) {
  U === null && es(), zs(() => () => _r(t));
}
function qo(t, e, n = !1) {
  var r;
  B && (r = N, or());
  var s = new Bo(t), i = n ? bt : 0;
  function o(a, l) {
    if (B) {
      var c = Hi(
        /** @type {TemplateNode} */
        r
      );
      if (a !== parseInt(c.substring(1))) {
        var h = ts();
        Te(h), s.anchor = h, at(!1), s.ensure(a, l), at(!0);
        return;
      }
    }
    s.ensure(a, l);
  }
  Rs(() => {
    var a = !1;
    e((l, c = 0) => {
      a = !0, o(c, l);
    }), a || o(-1, null);
  }, i);
}
function Po(t, e) {
  mo(() => {
    var n = t.getRootNode(), r = (
      /** @type {ShadowRoot} */
      n.host ? (
        /** @type {ShadowRoot} */
        n
      ) : (
        /** @type {Document} */
        n.head ?? /** @type {Document} */
        n.ownerDocument.head
      )
    );
    if (!r.querySelector("#" + e.hash)) {
      const s = hr("style");
      s.id = e.hash, s.textContent = e.code, r.appendChild(s);
    }
  });
}
const Ir = [...` 	
\r\f \v\uFEFF`];
function Do(t, e, n) {
  var r = "" + t;
  if (n) {
    for (var s of Object.keys(n))
      if (n[s])
        r = r ? r + " " + s : s;
      else if (r.length)
        for (var i = s.length, o = 0; (o = r.indexOf(s, o)) >= 0; ) {
          var a = o + i;
          (o === 0 || Ir.includes(r[o - 1])) && (a === r.length || Ir.includes(r[a])) ? r = (o === 0 ? "" : r.substring(0, o)) + r.substring(a + 1) : o = a;
        }
  }
  return r === "" ? null : r;
}
function Io(t, e, n, r, s, i) {
  var o = (
    /** @type {any} */
    t[Vn]
  );
  if (B || o !== n || o === void 0) {
    var a = Do(n, r, i);
    (!B || a !== t.getAttribute("class")) && (a == null ? t.removeAttribute("class") : t.className = a), t[Vn] = n;
  } else if (i && s !== i)
    for (var l in i) {
      var c = !!i[l];
      (s == null || c !== !!s[l]) && t.classList.toggle(l, c);
    }
  return i;
}
const Mo = Symbol("is custom element"), Fo = Symbol("is html");
function Uo(t, e, n, r) {
  var s = Vo(t);
  B && (s[e] = t.getAttribute(e)), s[e] !== (s[e] = n) && (n == null ? t.removeAttribute(e) : typeof n != "string" && Ho(t).includes(e) ? t[e] = n : t.setAttribute(e, n));
}
function Vo(t) {
  return (
    /** @type {Record<string | symbol, unknown>} **/
    /** @type {any} */
    t[Gt] ?? (t[Gt] = {
      [Mo]: t.nodeName.includes("-"),
      [Fo]: t.namespaceURI === yi
    })
  );
}
var Mr = /* @__PURE__ */ new Map();
function Ho(t) {
  var e = t.getAttribute("is") || t.nodeName, n = Mr.get(e);
  if (n) return n;
  Mr.set(e, n = []);
  for (var r, s = t, i = Element.prototype; i !== s; ) {
    r = bi(s);
    for (var o in r)
      r[o].set && // better safe than sorry, we don't want spread attributes to mess with HTML content
      o !== "innerHTML" && o !== "textContent" && o !== "innerText" && n.push(o);
    s = Qr(s);
  }
  return n;
}
function Dn(t, e, n, r) {
  var s = (
    /** @type {V} */
    r
  ), i = !0, o = () => (i && (i = !1, s = /** @type {V} */
  r), s), a;
  a = /** @type {V} */
  t[e], a === void 0 && r !== void 0 && (a = o());
  var l;
  l = () => {
    var u = (
      /** @type {V} */
      t[e]
    );
    return u === void 0 ? o() : (i = !0, u);
  };
  var c = !1, h = /* @__PURE__ */ fr(() => (c = !1, l())), p = (
    /** @type {Effect} */
    E
  );
  return (
    /** @type {() => V} */
    (function(u, d) {
      if (arguments.length > 0) {
        const _ = d ? x(h) : u;
        return R(h, _), c = !0, s !== void 0 && (s = _), u;
      }
      return qe && c || (p.f & ie) !== 0 ? h.v : x(h);
    })
  );
}
function jo(t) {
  return new Yo(t);
}
var Le, te;
class Yo {
  /**
   * @param {ComponentConstructorOptions & {
   *  component: any;
   * }} options
   */
  constructor(e) {
    /** @type {any} */
    w(this, Le);
    /** @type {Record<string, any>} */
    w(this, te);
    var i;
    var n = /* @__PURE__ */ new Map(), r = (o, a) => {
      var l = /* @__PURE__ */ fo(a, !1, !1);
      return n.set(o, l), l;
    };
    const s = new Proxy(
      { ...e.props || {}, $$events: {} },
      {
        get(o, a) {
          return x(n.get(a) ?? r(a, Reflect.get(o, a)));
        },
        has(o, a) {
          return a === Oi ? !0 : (x(n.get(a) ?? r(a, Reflect.get(o, a))), Reflect.has(o, a));
        },
        set(o, a, l) {
          return R(n.get(a) ?? r(a, l), l), Reflect.set(o, a, l);
        }
      }
    );
    y(this, te, (e.hydrate ? Co : js)(e.component, {
      target: e.target,
      anchor: e.anchor,
      props: s,
      context: e.context,
      intro: e.intro ?? !1,
      recover: e.recover,
      transformError: e.transformError
    })), (!((i = e == null ? void 0 : e.props) != null && i.$$host) || e.sync === !1) && nn(), y(this, Le, s.$$events);
    for (const o of Object.keys(f(this, te)))
      o === "$set" || o === "$destroy" || o === "$on" || dn(this, o, {
        get() {
          return f(this, te)[o];
        },
        /** @param {any} value */
        set(a) {
          f(this, te)[o] = a;
        },
        enumerable: !0
      });
    f(this, te).$set = /** @param {Record<string, any>} next */
    (o) => {
      Object.assign(s, o);
    }, f(this, te).$destroy = () => {
      No(f(this, te));
    };
  }
  /** @param {Record<string, any>} props */
  $set(e) {
    f(this, te).$set(e);
  }
  /**
   * @param {string} event
   * @param {(...args: any[]) => any} callback
   * @returns {any}
   */
  $on(e, n) {
    f(this, Le)[e] = f(this, Le)[e] || [];
    const r = (...s) => n.call(this, ...s);
    return f(this, Le)[e].push(r), () => {
      f(this, Le)[e] = f(this, Le)[e].filter(
        /** @param {any} fn */
        (s) => s !== r
      );
    };
  }
  $destroy() {
    f(this, te).$destroy();
  }
}
Le = new WeakMap(), te = new WeakMap();
let Ks;
typeof HTMLElement == "function" && (Ks = class extends HTMLElement {
  /**
   * @param {*} $$componentCtor
   * @param {*} $$slots
   * @param {ShadowRootInit | undefined} shadow_root_init
   */
  constructor(e, n, r) {
    super();
    /** The Svelte component constructor */
    O(this, "$$ctor");
    /** Slots */
    O(this, "$$s");
    /** @type {any} The Svelte component instance */
    O(this, "$$c");
    /** Whether or not the custom element is connected */
    O(this, "$$cn", !1);
    /** @type {Record<string, any>} Component props data */
    O(this, "$$d", {});
    /** `true` if currently in the process of reflecting component props back to attributes */
    O(this, "$$r", !1);
    /** @type {Record<string, CustomElementPropDefinition>} Props definition (name, reflected, type etc) */
    O(this, "$$p_d", {});
    /** @type {Record<string, EventListenerOrEventListenerObject[]>} Event listeners */
    O(this, "$$l", {});
    /** @type {Map<EventListenerOrEventListenerObject, Function>} Event listener unsubscribe functions */
    O(this, "$$l_u", /* @__PURE__ */ new Map());
    /** @type {any} The managed render effect for reflecting attributes */
    O(this, "$$me");
    /** @type {ShadowRoot | null} The ShadowRoot of the custom element */
    O(this, "$$shadowRoot", null);
    this.$$ctor = e, this.$$s = n, r && (this.$$shadowRoot = this.attachShadow(r));
  }
  /**
   * @param {string} type
   * @param {EventListenerOrEventListenerObject} listener
   * @param {boolean | AddEventListenerOptions} [options]
   */
  addEventListener(e, n, r) {
    if (this.$$l[e] = this.$$l[e] || [], this.$$l[e].push(n), this.$$c) {
      const s = this.$$c.$on(e, n);
      this.$$l_u.set(n, s);
    }
    super.addEventListener(e, n, r);
  }
  /**
   * @param {string} type
   * @param {EventListenerOrEventListenerObject} listener
   * @param {boolean | AddEventListenerOptions} [options]
   */
  removeEventListener(e, n, r) {
    if (super.removeEventListener(e, n, r), this.$$c) {
      const s = this.$$l_u.get(n);
      s && (s(), this.$$l_u.delete(n));
    }
  }
  async connectedCallback() {
    if (this.$$cn = !0, !this.$$c) {
      let n = function(i) {
        return (o) => {
          const a = hr("slot");
          i !== "default" && (a.name = i), Gn(o, a);
        };
      };
      var e = n;
      if (await Promise.resolve(), !this.$$cn || this.$$c)
        return;
      const r = {}, s = zo(this);
      for (const i of this.$$s)
        i in s && (i === "default" && !this.$$d.children ? (this.$$d.children = n(i), r.default = !0) : r[i] = n(i));
      for (const i of this.attributes) {
        const o = this.$$g_p(i.name);
        o in this.$$d || (this.$$d[o] = sn(o, i.value, this.$$p_d, "toProp"));
      }
      for (const i in this.$$p_d)
        !(i in this.$$d) && this[i] !== void 0 && (this.$$d[i] = this[i], delete this[i]);
      this.$$c = jo({
        component: this.$$ctor,
        target: this.$$shadowRoot || this,
        props: {
          ...this.$$d,
          $$slots: r,
          $$host: this
        }
      }), this.$$me = yo(() => {
        Os(() => {
          var i;
          this.$$r = !0;
          for (const o of hn(this.$$c)) {
            if (!((i = this.$$p_d[o]) != null && i.reflect)) continue;
            this.$$d[o] = this.$$c[o];
            const a = sn(
              o,
              this.$$d[o],
              this.$$p_d,
              "toAttribute"
            );
            a == null ? this.removeAttribute(this.$$p_d[o].attribute || o) : this.setAttribute(this.$$p_d[o].attribute || o, a);
          }
          this.$$r = !1;
        });
      });
      for (const i in this.$$l)
        for (const o of this.$$l[i]) {
          const a = this.$$c.$on(i, o);
          this.$$l_u.set(o, a);
        }
      this.$$l = {};
    }
  }
  // We don't need this when working within Svelte code, but for compatibility of people using this outside of Svelte
  // and setting attributes through setAttribute etc, this is helpful
  /**
   * @param {string} attr
   * @param {string} _oldValue
   * @param {string} newValue
   */
  attributeChangedCallback(e, n, r) {
    var s;
    this.$$r || (e = this.$$g_p(e), this.$$d[e] = sn(e, r, this.$$p_d, "toProp"), (s = this.$$c) == null || s.$set({ [e]: this.$$d[e] }));
  }
  disconnectedCallback() {
    this.$$cn = !1, Promise.resolve().then(() => {
      !this.$$cn && this.$$c && (this.$$c.$destroy(), this.$$me(), this.$$c = void 0);
    });
  }
  /**
   * @param {string} attribute_name
   */
  $$g_p(e) {
    return hn(this.$$p_d).find(
      (n) => this.$$p_d[n].attribute === e || !this.$$p_d[n].attribute && n.toLowerCase() === e
    ) || e;
  }
});
function sn(t, e, n, r) {
  var i;
  const s = (i = n[t]) == null ? void 0 : i.type;
  if (e = s === "Boolean" && typeof e != "boolean" ? e != null : e, !r || !n[t])
    return e;
  if (r === "toAttribute")
    switch (s) {
      case "Object":
      case "Array":
        return e == null ? null : JSON.stringify(e);
      case "Boolean":
        return e ? "" : null;
      case "Number":
        return e ?? null;
      default:
        return e;
    }
  else
    switch (s) {
      case "Object":
      case "Array":
        return e && JSON.parse(e);
      case "Boolean":
        return e;
      // conversion already handled above
      case "Number":
        return e != null ? +e : e;
      default:
        return e;
    }
}
function zo(t) {
  const e = {};
  return t.childNodes.forEach((n) => {
    e[
      /** @type {Element} node */
      n.slot || "default"
    ] = !0;
  }), e;
}
function Ko(t, e, n, r, s, i) {
  let o = class extends Ks {
    constructor() {
      super(t, n, s), this.$$p_d = e;
    }
    static get observedAttributes() {
      return hn(e).map(
        (a) => (e[a].attribute || a).toLowerCase()
      );
    }
  };
  return hn(e).forEach((a) => {
    dn(o.prototype, a, {
      get() {
        return this.$$c && a in this.$$c ? this.$$c[a] : this.$$d[a];
      },
      set(l) {
        var p;
        l = sn(a, l, e), this.$$d[a] = l;
        var c = this.$$c;
        if (c) {
          var h = (p = lt(c, a)) == null ? void 0 : p.get;
          h ? c[a] = l : c.$set({ [a]: l });
        }
      }
    });
  }), r.forEach((a) => {
    dn(o.prototype, a, {
      get() {
        var l;
        return (l = this.$$c) == null ? void 0 : l[a];
      }
    });
  }), t.element = /** @type {any} */
  o, o;
}
const Se = /* @__PURE__ */ Object.create(null);
Se.open = "0";
Se.close = "1";
Se.ping = "2";
Se.pong = "3";
Se.message = "4";
Se.upgrade = "5";
Se.noop = "6";
const on = /* @__PURE__ */ Object.create(null);
Object.keys(Se).forEach((t) => {
  on[Se[t]] = t;
});
const er = { type: "error", data: "parser error" }, Ws = typeof Blob == "function" || typeof Blob < "u" && Object.prototype.toString.call(Blob) === "[object BlobConstructor]", Js = typeof ArrayBuffer == "function", Xs = (t) => typeof ArrayBuffer.isView == "function" ? ArrayBuffer.isView(t) : t && t.buffer instanceof ArrayBuffer, vr = ({ type: t, data: e }, n, r) => Ws && e instanceof Blob ? n ? r(e) : Fr(e, r) : Js && (e instanceof ArrayBuffer || Xs(e)) ? n ? r(e) : Fr(new Blob([e]), r) : r(Se[t] + (e || "")), Fr = (t, e) => {
  const n = new FileReader();
  return n.onload = function() {
    const r = n.result.split(",")[1];
    e("b" + (r || ""));
  }, n.readAsDataURL(t);
};
function Ur(t) {
  return t instanceof Uint8Array ? t : t instanceof ArrayBuffer ? new Uint8Array(t) : new Uint8Array(t.buffer, t.byteOffset, t.byteLength);
}
let In;
function Wo(t, e) {
  if (Ws && t.data instanceof Blob)
    return t.data.arrayBuffer().then(Ur).then(e);
  if (Js && (t.data instanceof ArrayBuffer || Xs(t.data)))
    return e(Ur(t.data));
  vr(t, !1, (n) => {
    In || (In = new TextEncoder()), e(In.encode(n));
  });
}
const Vr = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", Nt = typeof Uint8Array > "u" ? [] : new Uint8Array(256);
for (let t = 0; t < Vr.length; t++)
  Nt[Vr.charCodeAt(t)] = t;
const Jo = (t) => {
  let e = t.length * 0.75, n = t.length, r, s = 0, i, o, a, l;
  t[t.length - 1] === "=" && (e--, t[t.length - 2] === "=" && e--);
  const c = new ArrayBuffer(e), h = new Uint8Array(c);
  for (r = 0; r < n; r += 4)
    i = Nt[t.charCodeAt(r)], o = Nt[t.charCodeAt(r + 1)], a = Nt[t.charCodeAt(r + 2)], l = Nt[t.charCodeAt(r + 3)], h[s++] = i << 2 | o >> 4, h[s++] = (o & 15) << 4 | a >> 2, h[s++] = (a & 3) << 6 | l & 63;
  return c;
}, Xo = typeof ArrayBuffer == "function", yr = (t, e) => {
  if (typeof t != "string")
    return {
      type: "message",
      data: Qs(t, e)
    };
  const n = t.charAt(0);
  return n === "b" ? {
    type: "message",
    data: Qo(t.substring(1), e)
  } : on[n] ? t.length > 1 ? {
    type: on[n],
    data: t.substring(1)
  } : {
    type: on[n]
  } : er;
}, Qo = (t, e) => {
  if (Xo) {
    const n = Jo(t);
    return Qs(n, e);
  } else
    return { base64: !0, data: t };
}, Qs = (t, e) => {
  switch (e) {
    case "blob":
      return t instanceof Blob ? t : new Blob([t]);
    case "arraybuffer":
    default:
      return t instanceof ArrayBuffer ? t : t.buffer;
  }
}, Gs = "", Go = (t, e) => {
  const n = t.length, r = new Array(n);
  let s = 0;
  t.forEach((i, o) => {
    vr(i, !1, (a) => {
      r[o] = a, ++s === n && e(r.join(Gs));
    });
  });
}, Zo = (t, e) => {
  const n = t.split(Gs), r = [];
  for (let s = 0; s < n.length; s++) {
    const i = yr(n[s], e);
    if (r.push(i), i.type === "error")
      break;
  }
  return r;
};
function ea() {
  return new TransformStream({
    transform(t, e) {
      Wo(t, (n) => {
        const r = n.length;
        let s;
        if (r < 126)
          s = new Uint8Array(1), new DataView(s.buffer).setUint8(0, r);
        else if (r < 65536) {
          s = new Uint8Array(3);
          const i = new DataView(s.buffer);
          i.setUint8(0, 126), i.setUint16(1, r);
        } else {
          s = new Uint8Array(9);
          const i = new DataView(s.buffer);
          i.setUint8(0, 127), i.setBigUint64(1, BigInt(r));
        }
        t.data && typeof t.data != "string" && (s[0] |= 128), e.enqueue(s), e.enqueue(n);
      });
    }
  });
}
let Mn;
function Xt(t) {
  return t.reduce((e, n) => e + n.length, 0);
}
function Qt(t, e) {
  if (t[0].length === e)
    return t.shift();
  const n = new Uint8Array(e);
  let r = 0;
  for (let s = 0; s < e; s++)
    n[s] = t[0][r++], r === t[0].length && (t.shift(), r = 0);
  return t.length && r < t[0].length && (t[0] = t[0].slice(r)), n;
}
function ta(t, e) {
  Mn || (Mn = new TextDecoder());
  const n = [];
  let r = 0, s = -1, i = !1;
  return new TransformStream({
    transform(o, a) {
      for (n.push(o); ; ) {
        if (r === 0) {
          if (Xt(n) < 1)
            break;
          const l = Qt(n, 1);
          i = (l[0] & 128) === 128, s = l[0] & 127, s < 126 ? r = 3 : s === 126 ? r = 1 : r = 2;
        } else if (r === 1) {
          if (Xt(n) < 2)
            break;
          const l = Qt(n, 2);
          s = new DataView(l.buffer, l.byteOffset, l.length).getUint16(0), r = 3;
        } else if (r === 2) {
          if (Xt(n) < 8)
            break;
          const l = Qt(n, 8), c = new DataView(l.buffer, l.byteOffset, l.length), h = c.getUint32(0);
          if (h > Math.pow(2, 21) - 1) {
            a.enqueue(er);
            break;
          }
          s = h * Math.pow(2, 32) + c.getUint32(4), r = 3;
        } else {
          if (Xt(n) < s)
            break;
          const l = Qt(n, s);
          a.enqueue(yr(i ? l : Mn.decode(l), e)), r = 0;
        }
        if (s === 0 || s > t) {
          a.enqueue(er);
          break;
        }
      }
    }
  });
}
const Zs = 4;
function P(t) {
  if (t) return na(t);
}
function na(t) {
  for (var e in P.prototype)
    t[e] = P.prototype[e];
  return t;
}
P.prototype.on = P.prototype.addEventListener = function(t, e) {
  return this._callbacks = this._callbacks || {}, (this._callbacks["$" + t] = this._callbacks["$" + t] || []).push(e), this;
};
P.prototype.once = function(t, e) {
  function n() {
    this.off(t, n), e.apply(this, arguments);
  }
  return n.fn = e, this.on(t, n), this;
};
P.prototype.off = P.prototype.removeListener = P.prototype.removeAllListeners = P.prototype.removeEventListener = function(t, e) {
  if (this._callbacks = this._callbacks || {}, arguments.length == 0)
    return this._callbacks = {}, this;
  var n = this._callbacks["$" + t];
  if (!n) return this;
  if (arguments.length == 1)
    return delete this._callbacks["$" + t], this;
  for (var r, s = 0; s < n.length; s++)
    if (r = n[s], r === e || r.fn === e) {
      n.splice(s, 1);
      break;
    }
  return n.length === 0 && delete this._callbacks["$" + t], this;
};
P.prototype.emit = function(t) {
  this._callbacks = this._callbacks || {};
  for (var e = new Array(arguments.length - 1), n = this._callbacks["$" + t], r = 1; r < arguments.length; r++)
    e[r - 1] = arguments[r];
  if (n) {
    n = n.slice(0);
    for (var r = 0, s = n.length; r < s; ++r)
      n[r].apply(this, e);
  }
  return this;
};
P.prototype.emitReserved = P.prototype.emit;
P.prototype.listeners = function(t) {
  return this._callbacks = this._callbacks || {}, this._callbacks["$" + t] || [];
};
P.prototype.hasListeners = function(t) {
  return !!this.listeners(t).length;
};
const An = typeof Promise == "function" && typeof Promise.resolve == "function" ? (e) => Promise.resolve().then(e) : (e, n) => n(e, 0), ne = typeof self < "u" ? self : typeof window < "u" ? window : Function("return this")(), ra = "arraybuffer";
function ei(t, ...e) {
  return e.reduce((n, r) => (t.hasOwnProperty(r) && (n[r] = t[r]), n), {});
}
const sa = ne.setTimeout, ia = ne.clearTimeout;
function On(t, e) {
  e.useNativeTimers ? (t.setTimeoutFn = sa.bind(ne), t.clearTimeoutFn = ia.bind(ne)) : (t.setTimeoutFn = ne.setTimeout.bind(ne), t.clearTimeoutFn = ne.clearTimeout.bind(ne));
}
const oa = 1.33;
function aa(t) {
  return typeof t == "string" ? fa(t) : Math.ceil((t.byteLength || t.size) * oa);
}
function fa(t) {
  let e = 0, n = 0;
  for (let r = 0, s = t.length; r < s; r++)
    e = t.charCodeAt(r), e < 128 ? n += 1 : e < 2048 ? n += 2 : e < 55296 || e >= 57344 ? n += 3 : (r++, n += 4);
  return n;
}
function ti() {
  return Date.now().toString(36).substring(3) + Math.random().toString(36).substring(2, 5);
}
function la(t) {
  let e = "";
  for (let n in t)
    t.hasOwnProperty(n) && (e.length && (e += "&"), e += encodeURIComponent(n) + "=" + encodeURIComponent(t[n]));
  return e;
}
function ca(t) {
  let e = {}, n = t.split("&");
  for (let r = 0, s = n.length; r < s; r++) {
    let i = n[r].split("=");
    e[decodeURIComponent(i[0])] = decodeURIComponent(i[1]);
  }
  return e;
}
class ua extends Error {
  constructor(e, n, r) {
    super(e), this.description = n, this.context = r, this.type = "TransportError";
  }
}
class gr extends P {
  /**
   * Transport abstract constructor.
   *
   * @param {Object} opts - options
   * @protected
   */
  constructor(e) {
    super(), this.writable = !1, On(this, e), this.opts = e, this.query = e.query, this.socket = e.socket, this.supportsBinary = !e.forceBase64;
  }
  /**
   * Emits an error.
   *
   * @param {String} reason
   * @param description
   * @param context - the error context
   * @return {Transport} for chaining
   * @protected
   */
  onError(e, n, r) {
    return super.emitReserved("error", new ua(e, n, r)), this;
  }
  /**
   * Opens the transport.
   */
  open() {
    return this.readyState = "opening", this.doOpen(), this;
  }
  /**
   * Closes the transport.
   */
  close() {
    return (this.readyState === "opening" || this.readyState === "open") && (this.doClose(), this.onClose()), this;
  }
  /**
   * Sends multiple packets.
   *
   * @param {Array} packets
   */
  send(e) {
    this.readyState === "open" && this.write(e);
  }
  /**
   * Called upon open
   *
   * @protected
   */
  onOpen() {
    this.readyState = "open", this.writable = !0, super.emitReserved("open");
  }
  /**
   * Called with data.
   *
   * @param {String} data
   * @protected
   */
  onData(e) {
    const n = yr(e, this.socket.binaryType);
    this.onPacket(n);
  }
  /**
   * Called with a decoded packet.
   *
   * @protected
   */
  onPacket(e) {
    super.emitReserved("packet", e);
  }
  /**
   * Called upon close.
   *
   * @protected
   */
  onClose(e) {
    this.readyState = "closed", super.emitReserved("close", e);
  }
  /**
   * Pauses the transport, in order not to lose packets during an upgrade.
   *
   * @param onPause
   */
  pause(e) {
  }
  createUri(e, n = {}) {
    return e + "://" + this._hostname() + this._port() + this.opts.path + this._query(n);
  }
  _hostname() {
    const e = this.opts.hostname;
    return e.indexOf(":") === -1 ? e : "[" + e + "]";
  }
  _port() {
    return this.opts.port && (this.opts.secure && Number(this.opts.port) !== 443 || !this.opts.secure && Number(this.opts.port) !== 80) ? ":" + this.opts.port : "";
  }
  _query(e) {
    const n = la(e);
    return n.length ? "?" + n : "";
  }
}
class ha extends gr {
  constructor() {
    super(...arguments), this._polling = !1;
  }
  get name() {
    return "polling";
  }
  /**
   * Opens the socket (triggers polling). We write a PING message to determine
   * when the transport is open.
   *
   * @protected
   */
  doOpen() {
    this._poll();
  }
  /**
   * Pauses polling.
   *
   * @param {Function} onPause - callback upon buffers are flushed and transport is paused
   * @package
   */
  pause(e) {
    this.readyState = "pausing";
    const n = () => {
      this.readyState = "paused", e();
    };
    if (this._polling || !this.writable) {
      let r = 0;
      this._polling && (r++, this.once("pollComplete", function() {
        --r || n();
      })), this.writable || (r++, this.once("drain", function() {
        --r || n();
      }));
    } else
      n();
  }
  /**
   * Starts polling cycle.
   *
   * @private
   */
  _poll() {
    this._polling = !0, this.doPoll(), this.emitReserved("poll");
  }
  /**
   * Overloads onData to detect payloads.
   *
   * @protected
   */
  onData(e) {
    const n = (r) => {
      if (this.readyState === "opening" && r.type === "open" && this.onOpen(), r.type === "close")
        return this.onClose({ description: "transport closed by the server" }), !1;
      this.onPacket(r);
    };
    Zo(e, this.socket.binaryType).forEach(n), this.readyState !== "closed" && (this._polling = !1, this.emitReserved("pollComplete"), this.readyState === "open" && this._poll());
  }
  /**
   * For polling, send a close packet.
   *
   * @protected
   */
  doClose() {
    const e = () => {
      this.write([{ type: "close" }]);
    };
    this.readyState === "open" ? e() : this.once("open", e);
  }
  /**
   * Writes a packets payload.
   *
   * @param {Array} packets - data packets
   * @protected
   */
  write(e) {
    this.writable = !1, Go(e, (n) => {
      this.doWrite(n, () => {
        this.writable = !0, this.emitReserved("drain");
      });
    });
  }
  /**
   * Generates uri for connection.
   *
   * @private
   */
  uri() {
    const e = this.opts.secure ? "https" : "http", n = this.query || {};
    return this.opts.timestampRequests !== !1 && (n[this.opts.timestampParam] = ti()), !this.supportsBinary && !n.sid && (n.b64 = 1), this.createUri(e, n);
  }
}
let ni = !1;
try {
  ni = typeof XMLHttpRequest < "u" && "withCredentials" in new XMLHttpRequest();
} catch {
}
const da = ni;
function pa() {
}
class _a extends ha {
  /**
   * XHR Polling constructor.
   *
   * @param {Object} opts
   * @package
   */
  constructor(e) {
    if (super(e), typeof location < "u") {
      const n = location.protocol === "https:";
      let r = location.port;
      r || (r = n ? "443" : "80"), this.xd = typeof location < "u" && e.hostname !== location.hostname || r !== e.port;
    }
  }
  /**
   * Sends data.
   *
   * @param {String} data - data to send.
   * @param {Function} fn - called upon flush.
   * @private
   */
  doWrite(e, n) {
    const r = this.request({
      method: "POST",
      data: e
    });
    r.on("success", n), r.on("error", (s, i) => {
      this.onError("xhr post error", s, i);
    });
  }
  /**
   * Starts a poll cycle.
   *
   * @private
   */
  doPoll() {
    const e = this.request();
    e.on("data", this.onData.bind(this)), e.on("error", (n, r) => {
      this.onError("xhr poll error", n, r);
    }), this.pollXhr = e;
  }
}
class Ee extends P {
  /**
   * Request constructor
   *
   * @param {Object} options
   * @package
   */
  constructor(e, n, r) {
    super(), this.createRequest = e, On(this, r), this._opts = r, this._method = r.method || "GET", this._uri = n, this._data = r.data !== void 0 ? r.data : null, this._create();
  }
  /**
   * Creates the XHR object and sends the request.
   *
   * @private
   */
  _create() {
    var e;
    const n = ei(this._opts, "agent", "pfx", "key", "passphrase", "cert", "ca", "ciphers", "rejectUnauthorized", "autoUnref");
    n.xdomain = !!this._opts.xd;
    const r = this._xhr = this.createRequest(n);
    try {
      r.open(this._method, this._uri, !0);
      try {
        if (this._opts.extraHeaders) {
          r.setDisableHeaderCheck && r.setDisableHeaderCheck(!0);
          for (let s in this._opts.extraHeaders)
            this._opts.extraHeaders.hasOwnProperty(s) && r.setRequestHeader(s, this._opts.extraHeaders[s]);
        }
      } catch {
      }
      if (this._method === "POST")
        try {
          r.setRequestHeader("Content-type", "text/plain;charset=UTF-8");
        } catch {
        }
      try {
        r.setRequestHeader("Accept", "*/*");
      } catch {
      }
      (e = this._opts.cookieJar) === null || e === void 0 || e.addCookies(r), "withCredentials" in r && (r.withCredentials = this._opts.withCredentials), this._opts.requestTimeout && (r.timeout = this._opts.requestTimeout), r.onreadystatechange = () => {
        var s;
        r.readyState === 3 && ((s = this._opts.cookieJar) === null || s === void 0 || s.parseCookies(
          // @ts-ignore
          r.getResponseHeader("set-cookie")
        )), r.readyState === 4 && (r.status === 200 || r.status === 1223 ? this._onLoad() : this.setTimeoutFn(() => {
          this._onError(typeof r.status == "number" ? r.status : 0);
        }, 0));
      }, r.send(this._data);
    } catch (s) {
      this.setTimeoutFn(() => {
        this._onError(s);
      }, 0);
      return;
    }
    typeof document < "u" && (this._index = Ee.requestsCount++, Ee.requests[this._index] = this);
  }
  /**
   * Called upon error.
   *
   * @private
   */
  _onError(e) {
    this.emitReserved("error", e, this._xhr), this._cleanup(!0);
  }
  /**
   * Cleans up house.
   *
   * @private
   */
  _cleanup(e) {
    if (!(typeof this._xhr > "u" || this._xhr === null)) {
      if (this._xhr.onreadystatechange = pa, e)
        try {
          this._xhr.abort();
        } catch {
        }
      typeof document < "u" && delete Ee.requests[this._index], this._xhr = null;
    }
  }
  /**
   * Called upon load.
   *
   * @private
   */
  _onLoad() {
    const e = this._xhr.responseText;
    e !== null && (this.emitReserved("data", e), this.emitReserved("success"), this._cleanup());
  }
  /**
   * Aborts the request.
   *
   * @package
   */
  abort() {
    this._cleanup();
  }
}
Ee.requestsCount = 0;
Ee.requests = {};
if (typeof document < "u") {
  if (typeof attachEvent == "function")
    attachEvent("onunload", Hr);
  else if (typeof addEventListener == "function") {
    const t = "onpagehide" in ne ? "pagehide" : "unload";
    addEventListener(t, Hr, !1);
  }
}
function Hr() {
  for (let t in Ee.requests)
    Ee.requests.hasOwnProperty(t) && Ee.requests[t].abort();
}
const va = (function() {
  const t = ri({
    xdomain: !1
  });
  return t && t.responseType !== null;
})();
class ya extends _a {
  constructor(e) {
    super(e);
    const n = e && e.forceBase64;
    this.supportsBinary = va && !n;
  }
  request(e = {}) {
    return Object.assign(e, { xd: this.xd }, this.opts), new Ee(ri, this.uri(), e);
  }
}
function ri(t) {
  const e = t.xdomain;
  try {
    if (typeof XMLHttpRequest < "u" && (!e || da))
      return new XMLHttpRequest();
  } catch {
  }
  if (!e)
    try {
      return new ne[["Active"].concat("Object").join("X")]("Microsoft.XMLHTTP");
    } catch {
    }
}
const si = typeof navigator < "u" && typeof navigator.product == "string" && navigator.product.toLowerCase() === "reactnative";
class ga extends gr {
  get name() {
    return "websocket";
  }
  doOpen() {
    const e = this.uri(), n = this.opts.protocols, r = si ? {} : ei(this.opts, "agent", "perMessageDeflate", "pfx", "key", "passphrase", "cert", "ca", "ciphers", "rejectUnauthorized", "localAddress", "protocolVersion", "origin", "maxPayload", "family", "checkServerIdentity");
    this.opts.extraHeaders && (r.headers = this.opts.extraHeaders);
    try {
      this.ws = this.createSocket(e, n, r);
    } catch (s) {
      return this.emitReserved("error", s);
    }
    this.ws.binaryType = this.socket.binaryType, this.addEventListeners();
  }
  /**
   * Adds event listeners to the socket
   *
   * @private
   */
  addEventListeners() {
    this.ws.onopen = () => {
      this.opts.autoUnref && this.ws._socket.unref(), this.onOpen();
    }, this.ws.onclose = (e) => this.onClose({
      description: "websocket connection closed",
      context: e
    }), this.ws.onmessage = (e) => this.onData(e.data), this.ws.onerror = (e) => this.onError("websocket error", e);
  }
  write(e) {
    this.writable = !1;
    for (let n = 0; n < e.length; n++) {
      const r = e[n], s = n === e.length - 1;
      vr(r, this.supportsBinary, (i) => {
        try {
          this.doWrite(r, i);
        } catch {
        }
        s && An(() => {
          this.writable = !0, this.emitReserved("drain");
        }, this.setTimeoutFn);
      });
    }
  }
  doClose() {
    typeof this.ws < "u" && (this.ws.onerror = () => {
    }, this.ws.close(), this.ws = null);
  }
  /**
   * Generates uri for connection.
   *
   * @private
   */
  uri() {
    const e = this.opts.secure ? "wss" : "ws", n = this.query || {};
    return this.opts.timestampRequests && (n[this.opts.timestampParam] = ti()), this.supportsBinary || (n.b64 = 1), this.createUri(e, n);
  }
}
const Fn = ne.WebSocket || ne.MozWebSocket;
class ma extends ga {
  createSocket(e, n, r) {
    return si ? new Fn(e, n, r) : n ? new Fn(e, n) : new Fn(e);
  }
  doWrite(e, n) {
    this.ws.send(n);
  }
}
class wa extends gr {
  get name() {
    return "webtransport";
  }
  doOpen() {
    try {
      this._transport = new WebTransport(this.createUri("https"), this.opts.transportOptions[this.name]);
    } catch (e) {
      return this.emitReserved("error", e);
    }
    this._transport.closed.then(() => {
      this.onClose();
    }).catch((e) => {
      this.onError("webtransport error", e);
    }), this._transport.ready.then(() => {
      this._transport.createBidirectionalStream().then((e) => {
        const n = ta(Number.MAX_SAFE_INTEGER, this.socket.binaryType), r = e.readable.pipeThrough(n).getReader(), s = ea();
        s.readable.pipeTo(e.writable), this._writer = s.writable.getWriter();
        const i = () => {
          r.read().then(({ done: a, value: l }) => {
            a || (this.onPacket(l), i());
          }).catch((a) => {
          });
        };
        i();
        const o = { type: "open" };
        this.query.sid && (o.data = `{"sid":"${this.query.sid}"}`), this._writer.write(o).then(() => this.onOpen());
      });
    });
  }
  write(e) {
    this.writable = !1;
    for (let n = 0; n < e.length; n++) {
      const r = e[n], s = n === e.length - 1;
      this._writer.write(r).then(() => {
        s && An(() => {
          this.writable = !0, this.emitReserved("drain");
        }, this.setTimeoutFn);
      });
    }
  }
  doClose() {
    var e;
    (e = this._transport) === null || e === void 0 || e.close();
  }
}
const ba = {
  websocket: ma,
  webtransport: wa,
  polling: ya
}, Ea = /^(?:(?![^:@\/?#]+:[^:@\/]*@)(http|https|ws|wss):\/\/)?((?:(([^:@\/?#]*)(?::([^:@\/?#]*))?)?@)?((?:[a-f0-9]{0,4}:){2,7}[a-f0-9]{0,4}|[^:\/?#]*)(?::(\d*))?)(((\/(?:[^?#](?![^?#\/]*\.[^?#\/.]+(?:[?#]|$)))*\/?)?([^?#\/]*))(?:\?([^#]*))?(?:#(.*))?)/, ka = [
  "source",
  "protocol",
  "authority",
  "userInfo",
  "user",
  "password",
  "host",
  "port",
  "relative",
  "path",
  "directory",
  "file",
  "query",
  "anchor"
];
function tr(t) {
  if (t.length > 8e3)
    throw "URI too long";
  const e = t, n = t.indexOf("["), r = t.indexOf("]");
  n != -1 && r != -1 && (t = t.substring(0, n) + t.substring(n, r).replace(/:/g, ";") + t.substring(r, t.length));
  let s = Ea.exec(t || ""), i = {}, o = 14;
  for (; o--; )
    i[ka[o]] = s[o] || "";
  return n != -1 && r != -1 && (i.source = e, i.host = i.host.substring(1, i.host.length - 1).replace(/;/g, ":"), i.authority = i.authority.replace("[", "").replace("]", "").replace(/;/g, ":"), i.ipv6uri = !0), i.pathNames = Ta(i, i.path), i.queryKey = $a(i, i.query), i;
}
function Ta(t, e) {
  const n = /\/{2,9}/g, r = e.replace(n, "/").split("/");
  return (e.slice(0, 1) == "/" || e.length === 0) && r.splice(0, 1), e.slice(-1) == "/" && r.splice(r.length - 1, 1), r;
}
function $a(t, e) {
  const n = {};
  return e.replace(/(?:^|&)([^&=]*)=?([^&]*)/g, function(r, s, i) {
    s && (n[s] = i);
  }), n;
}
const nr = typeof addEventListener == "function" && typeof removeEventListener == "function", an = [];
nr && addEventListener("offline", () => {
  an.forEach((t) => t());
}, !1);
class Ue extends P {
  /**
   * Socket constructor.
   *
   * @param {String|Object} uri - uri or options
   * @param {Object} opts - options
   */
  constructor(e, n) {
    if (super(), this.binaryType = ra, this.writeBuffer = [], this._prevBufferLen = 0, this._pingInterval = -1, this._pingTimeout = -1, this._maxPayload = -1, this._pingTimeoutTime = 1 / 0, e && typeof e == "object" && (n = e, e = null), e) {
      const r = tr(e);
      n.hostname = r.host, n.secure = r.protocol === "https" || r.protocol === "wss", n.port = r.port, r.query && (n.query = r.query);
    } else n.host && (n.hostname = tr(n.host).host);
    On(this, n), this.secure = n.secure != null ? n.secure : typeof location < "u" && location.protocol === "https:", n.hostname && !n.port && (n.port = this.secure ? "443" : "80"), this.hostname = n.hostname || (typeof location < "u" ? location.hostname : "localhost"), this.port = n.port || (typeof location < "u" && location.port ? location.port : this.secure ? "443" : "80"), this.transports = [], this._transportsByName = {}, n.transports.forEach((r) => {
      const s = r.prototype.name;
      this.transports.push(s), this._transportsByName[s] = r;
    }), this.opts = Object.assign({
      path: "/engine.io",
      agent: !1,
      withCredentials: !1,
      upgrade: !0,
      timestampParam: "t",
      rememberUpgrade: !1,
      addTrailingSlash: !0,
      rejectUnauthorized: !0,
      perMessageDeflate: {
        threshold: 1024
      },
      transportOptions: {},
      closeOnBeforeunload: !1
    }, n), this.opts.path = this.opts.path.replace(/\/$/, "") + (this.opts.addTrailingSlash ? "/" : ""), typeof this.opts.query == "string" && (this.opts.query = ca(this.opts.query)), nr && (this.opts.closeOnBeforeunload && (this._beforeunloadEventListener = () => {
      this.transport && (this.transport.removeAllListeners(), this.transport.close());
    }, addEventListener("beforeunload", this._beforeunloadEventListener, !1)), this.hostname !== "localhost" && (this._offlineEventListener = () => {
      this._onClose("transport close", {
        description: "network connection lost"
      });
    }, an.push(this._offlineEventListener))), this.opts.withCredentials && (this._cookieJar = void 0), this._open();
  }
  /**
   * Creates transport of the given type.
   *
   * @param {String} name - transport name
   * @return {Transport}
   * @private
   */
  createTransport(e) {
    const n = Object.assign({}, this.opts.query);
    n.EIO = Zs, n.transport = e, this.id && (n.sid = this.id);
    const r = Object.assign({}, this.opts, {
      query: n,
      socket: this,
      hostname: this.hostname,
      secure: this.secure,
      port: this.port
    }, this.opts.transportOptions[e]);
    return new this._transportsByName[e](r);
  }
  /**
   * Initializes transport to use and starts probe.
   *
   * @private
   */
  _open() {
    if (this.transports.length === 0) {
      this.setTimeoutFn(() => {
        this.emitReserved("error", "No transports available");
      }, 0);
      return;
    }
    const e = this.opts.rememberUpgrade && Ue.priorWebsocketSuccess && this.transports.indexOf("websocket") !== -1 ? "websocket" : this.transports[0];
    this.readyState = "opening";
    const n = this.createTransport(e);
    n.open(), this.setTransport(n);
  }
  /**
   * Sets the current transport. Disables the existing one (if any).
   *
   * @private
   */
  setTransport(e) {
    this.transport && this.transport.removeAllListeners(), this.transport = e, e.on("drain", this._onDrain.bind(this)).on("packet", this._onPacket.bind(this)).on("error", this._onError.bind(this)).on("close", (n) => this._onClose("transport close", n));
  }
  /**
   * Called when connection is deemed open.
   *
   * @private
   */
  onOpen() {
    this.readyState = "open", Ue.priorWebsocketSuccess = this.transport.name === "websocket", this.emitReserved("open"), this.flush();
  }
  /**
   * Handles a packet.
   *
   * @private
   */
  _onPacket(e) {
    if (this.readyState === "opening" || this.readyState === "open" || this.readyState === "closing")
      switch (this.emitReserved("packet", e), this.emitReserved("heartbeat"), e.type) {
        case "open":
          this.onHandshake(JSON.parse(e.data));
          break;
        case "ping":
          this._sendPacket("pong"), this.emitReserved("ping"), this.emitReserved("pong"), this._resetPingTimeout();
          break;
        case "error":
          const n = new Error("server error");
          n.code = e.data, this._onError(n);
          break;
        case "message":
          this.emitReserved("data", e.data), this.emitReserved("message", e.data);
          break;
      }
  }
  /**
   * Called upon handshake completion.
   *
   * @param {Object} data - handshake obj
   * @private
   */
  onHandshake(e) {
    this.emitReserved("handshake", e), this.id = e.sid, this.transport.query.sid = e.sid, this._pingInterval = e.pingInterval, this._pingTimeout = e.pingTimeout, this._maxPayload = e.maxPayload, this.onOpen(), this.readyState !== "closed" && this._resetPingTimeout();
  }
  /**
   * Sets and resets ping timeout timer based on server pings.
   *
   * @private
   */
  _resetPingTimeout() {
    this.clearTimeoutFn(this._pingTimeoutTimer);
    const e = this._pingInterval + this._pingTimeout;
    this._pingTimeoutTime = Date.now() + e, this._pingTimeoutTimer = this.setTimeoutFn(() => {
      this._onClose("ping timeout");
    }, e), this.opts.autoUnref && this._pingTimeoutTimer.unref();
  }
  /**
   * Called on `drain` event
   *
   * @private
   */
  _onDrain() {
    this.writeBuffer.splice(0, this._prevBufferLen), this._prevBufferLen = 0, this.writeBuffer.length === 0 ? this.emitReserved("drain") : this.flush();
  }
  /**
   * Flush write buffers.
   *
   * @private
   */
  flush() {
    if (this.readyState !== "closed" && this.transport.writable && !this.upgrading && this.writeBuffer.length) {
      const e = this._getWritablePackets();
      this.transport.send(e), this._prevBufferLen = e.length, this.emitReserved("flush");
    }
  }
  /**
   * Ensure the encoded size of the writeBuffer is below the maxPayload value sent by the server (only for HTTP
   * long-polling)
   *
   * @private
   */
  _getWritablePackets() {
    if (!(this._maxPayload && this.transport.name === "polling" && this.writeBuffer.length > 1))
      return this.writeBuffer;
    let n = 1;
    for (let r = 0; r < this.writeBuffer.length; r++) {
      const s = this.writeBuffer[r].data;
      if (s && (n += aa(s)), r > 0 && n > this._maxPayload)
        return this.writeBuffer.slice(0, r);
      n += 2;
    }
    return this.writeBuffer;
  }
  /**
   * Checks whether the heartbeat timer has expired but the socket has not yet been notified.
   *
   * Note: this method is private for now because it does not really fit the WebSocket API, but if we put it in the
   * `write()` method then the message would not be buffered by the Socket.IO client.
   *
   * @return {boolean}
   * @private
   */
  /* private */
  _hasPingExpired() {
    if (!this._pingTimeoutTime)
      return !0;
    const e = Date.now() > this._pingTimeoutTime;
    return e && (this._pingTimeoutTime = 0, An(() => {
      this._onClose("ping timeout");
    }, this.setTimeoutFn)), e;
  }
  /**
   * Sends a message.
   *
   * @param {String} msg - message.
   * @param {Object} options.
   * @param {Function} fn - callback function.
   * @return {Socket} for chaining.
   */
  write(e, n, r) {
    return this._sendPacket("message", e, n, r), this;
  }
  /**
   * Sends a message. Alias of {@link Socket#write}.
   *
   * @param {String} msg - message.
   * @param {Object} options.
   * @param {Function} fn - callback function.
   * @return {Socket} for chaining.
   */
  send(e, n, r) {
    return this._sendPacket("message", e, n, r), this;
  }
  /**
   * Sends a packet.
   *
   * @param {String} type - packet type.
   * @param {String} data.
   * @param {Object} options.
   * @param {Function} fn - callback function.
   * @private
   */
  _sendPacket(e, n, r, s) {
    if (typeof n == "function" && (s = n, n = void 0), typeof r == "function" && (s = r, r = null), this.readyState === "closing" || this.readyState === "closed")
      return;
    r = r || {}, r.compress = r.compress !== !1;
    const i = {
      type: e,
      data: n,
      options: r
    };
    this.emitReserved("packetCreate", i), this.writeBuffer.push(i), s && this.once("flush", s), this.flush();
  }
  /**
   * Closes the connection.
   */
  close() {
    const e = () => {
      this._onClose("forced close"), this.transport.close();
    }, n = () => {
      this.off("upgrade", n), this.off("upgradeError", n), e();
    }, r = () => {
      this.once("upgrade", n), this.once("upgradeError", n);
    };
    return (this.readyState === "opening" || this.readyState === "open") && (this.readyState = "closing", this.writeBuffer.length ? this.once("drain", () => {
      this.upgrading ? r() : e();
    }) : this.upgrading ? r() : e()), this;
  }
  /**
   * Called upon transport error
   *
   * @private
   */
  _onError(e) {
    if (Ue.priorWebsocketSuccess = !1, this.opts.tryAllTransports && this.transports.length > 1 && this.readyState === "opening")
      return this.transports.shift(), this._open();
    this.emitReserved("error", e), this._onClose("transport error", e);
  }
  /**
   * Called upon transport close.
   *
   * @private
   */
  _onClose(e, n) {
    if (this.readyState === "opening" || this.readyState === "open" || this.readyState === "closing") {
      if (this.clearTimeoutFn(this._pingTimeoutTimer), this.transport.removeAllListeners("close"), this.transport.close(), this.transport.removeAllListeners(), nr && (this._beforeunloadEventListener && removeEventListener("beforeunload", this._beforeunloadEventListener, !1), this._offlineEventListener)) {
        const r = an.indexOf(this._offlineEventListener);
        r !== -1 && an.splice(r, 1);
      }
      this.readyState = "closed", this.id = null, this.emitReserved("close", e, n), this.writeBuffer = [], this._prevBufferLen = 0;
    }
  }
}
Ue.protocol = Zs;
class Sa extends Ue {
  constructor() {
    super(...arguments), this._upgrades = [];
  }
  onOpen() {
    if (super.onOpen(), this.readyState === "open" && this.opts.upgrade)
      for (let e = 0; e < this._upgrades.length; e++)
        this._probe(this._upgrades[e]);
  }
  /**
   * Probes a transport.
   *
   * @param {String} name - transport name
   * @private
   */
  _probe(e) {
    let n = this.createTransport(e), r = !1;
    Ue.priorWebsocketSuccess = !1;
    const s = () => {
      r || (n.send([{ type: "ping", data: "probe" }]), n.once("packet", (p) => {
        if (!r)
          if (p.type === "pong" && p.data === "probe") {
            if (this.upgrading = !0, this.emitReserved("upgrading", n), !n)
              return;
            Ue.priorWebsocketSuccess = n.name === "websocket", this.transport.pause(() => {
              r || this.readyState !== "closed" && (h(), this.setTransport(n), n.send([{ type: "upgrade" }]), this.emitReserved("upgrade", n), n = null, this.upgrading = !1, this.flush());
            });
          } else {
            const u = new Error("probe error");
            u.transport = n.name, this.emitReserved("upgradeError", u);
          }
      }));
    };
    function i() {
      r || (r = !0, h(), n.close(), n = null);
    }
    const o = (p) => {
      const u = new Error("probe error: " + p);
      u.transport = n.name, i(), this.emitReserved("upgradeError", u);
    };
    function a() {
      o("transport closed");
    }
    function l() {
      o("socket closed");
    }
    function c(p) {
      n && p.name !== n.name && i();
    }
    const h = () => {
      n.removeListener("open", s), n.removeListener("error", o), n.removeListener("close", a), this.off("close", l), this.off("upgrading", c);
    };
    n.once("open", s), n.once("error", o), n.once("close", a), this.once("close", l), this.once("upgrading", c), this._upgrades.indexOf("webtransport") !== -1 && e !== "webtransport" ? this.setTimeoutFn(() => {
      r || n.open();
    }, 200) : n.open();
  }
  onHandshake(e) {
    this._upgrades = this._filterUpgrades(e.upgrades), super.onHandshake(e);
  }
  /**
   * Filters upgrades, returning only those matching client transports.
   *
   * @param {Array} upgrades - server upgrades
   * @private
   */
  _filterUpgrades(e) {
    const n = [];
    for (let r = 0; r < e.length; r++)
      ~this.transports.indexOf(e[r]) && n.push(e[r]);
    return n;
  }
}
let Aa = class extends Sa {
  constructor(e, n = {}) {
    const r = typeof e == "object", s = r ? { ...e } : { ...n };
    (!s.transports || s.transports && typeof s.transports[0] == "string") && (s.transports = (s.transports || ["polling", "websocket", "webtransport"]).map((i) => ba[i]).filter((i) => !!i)), super(r ? s : e, s);
  }
};
function Oa(t, e = "", n) {
  let r = t;
  n = n || typeof location < "u" && location, t == null && (t = n.protocol + "//" + n.host), typeof t == "string" && (t.charAt(0) === "/" && (t.charAt(1) === "/" ? t = n.protocol + t : t = n.host + t), /^(https?|wss?):\/\//.test(t) || (typeof n < "u" ? t = n.protocol + "//" + t : t = "https://" + t), r = tr(t)), r.port || (/^(http|ws)$/.test(r.protocol) ? r.port = "80" : /^(http|ws)s$/.test(r.protocol) && (r.port = "443")), r.path = r.path || "/";
  const i = r.host.indexOf(":") !== -1 ? "[" + r.host + "]" : r.host;
  return r.id = r.protocol + "://" + i + ":" + r.port + e, r.href = r.protocol + "://" + i + (n && n.port === r.port ? "" : ":" + r.port), r;
}
const Ra = typeof ArrayBuffer == "function", xa = (t) => typeof ArrayBuffer.isView == "function" ? ArrayBuffer.isView(t) : t.buffer instanceof ArrayBuffer, ii = Object.prototype.toString, Ca = typeof Blob == "function" || typeof Blob < "u" && ii.call(Blob) === "[object BlobConstructor]", Na = typeof File == "function" || typeof File < "u" && ii.call(File) === "[object FileConstructor]";
function mr(t) {
  return Ra && (t instanceof ArrayBuffer || xa(t)) || Ca && t instanceof Blob || Na && t instanceof File;
}
function fn(t, e) {
  if (!t || typeof t != "object")
    return !1;
  if (Array.isArray(t)) {
    for (let n = 0, r = t.length; n < r; n++)
      if (fn(t[n]))
        return !0;
    return !1;
  }
  if (mr(t))
    return !0;
  if (t.toJSON && typeof t.toJSON == "function" && arguments.length === 1)
    return fn(t.toJSON(), !0);
  for (const n in t)
    if (Object.prototype.hasOwnProperty.call(t, n) && fn(t[n]))
      return !0;
  return !1;
}
function Ba(t) {
  const e = [], n = t.data, r = t;
  return r.data = ln(n, e), r.attachments = e.length, { packet: r, buffers: e };
}
function ln(t, e, n) {
  if (!t)
    return t;
  if (mr(t)) {
    const r = { _placeholder: !0, num: e.length };
    return e.push(t), r;
  } else if (Array.isArray(t)) {
    const r = new Array(t.length);
    for (let s = 0; s < t.length; s++)
      r[s] = ln(t[s], e);
    return r;
  } else if (typeof t == "object" && !(t instanceof Date)) {
    if (t.toJSON && typeof t.toJSON == "function" && !n)
      return ln(t.toJSON(), e, !0);
    const r = {};
    for (const s in t)
      Object.prototype.hasOwnProperty.call(t, s) && (r[s] = ln(t[s], e));
    return r;
  }
  return t;
}
function La(t, e) {
  return t.data = rr(t.data, e), delete t.attachments, t;
}
function rr(t, e) {
  if (!t)
    return t;
  if (t && t._placeholder === !0) {
    if (typeof t.num == "number" && t.num >= 0 && t.num < e.length)
      return e[t.num];
    throw new Error("illegal attachments");
  } else if (Array.isArray(t))
    for (let n = 0; n < t.length; n++)
      t[n] = rr(t[n], e);
  else if (typeof t == "object")
    for (const n in t)
      Object.prototype.hasOwnProperty.call(t, n) && (t[n] = rr(t[n], e));
  return t;
}
const qa = [
  "connect",
  // used on the client side
  "connect_error",
  // used on the client side
  "disconnect",
  // used on both sides
  "disconnecting",
  // used on the server side
  "newListener",
  // used by the Node.js EventEmitter
  "removeListener"
  // used by the Node.js EventEmitter
];
var T;
(function(t) {
  t[t.CONNECT = 0] = "CONNECT", t[t.DISCONNECT = 1] = "DISCONNECT", t[t.EVENT = 2] = "EVENT", t[t.ACK = 3] = "ACK", t[t.CONNECT_ERROR = 4] = "CONNECT_ERROR", t[t.BINARY_EVENT = 5] = "BINARY_EVENT", t[t.BINARY_ACK = 6] = "BINARY_ACK";
})(T || (T = {}));
class Pa {
  /**
   * Encoder constructor
   *
   * @param {function} replacer - custom replacer to pass down to JSON.parse
   */
  constructor(e) {
    this.replacer = e;
  }
  /**
   * Encode a packet as a single string if non-binary, or as a
   * buffer sequence, depending on packet type.
   *
   * @param {Object} obj - packet object
   */
  encode(e) {
    return (e.type === T.EVENT || e.type === T.ACK) && fn(e) ? this.encodeAsBinary({
      type: e.type === T.EVENT ? T.BINARY_EVENT : T.BINARY_ACK,
      nsp: e.nsp,
      data: e.data,
      id: e.id
    }) : [this.encodeAsString(e)];
  }
  /**
   * Encode packet as string.
   */
  encodeAsString(e) {
    let n = "" + e.type;
    return (e.type === T.BINARY_EVENT || e.type === T.BINARY_ACK) && (n += e.attachments + "-"), e.nsp && e.nsp !== "/" && (n += e.nsp + ","), e.id != null && (n += e.id), e.data != null && (n += JSON.stringify(e.data, this.replacer)), n;
  }
  /**
   * Encode packet as 'buffer sequence' by removing blobs, and
   * deconstructing packet into object with placeholders and
   * a list of buffers.
   */
  encodeAsBinary(e) {
    const n = Ba(e), r = this.encodeAsString(n.packet), s = n.buffers;
    return s.unshift(r), s;
  }
}
class wr extends P {
  /**
   * Decoder constructor
   */
  constructor(e) {
    super(), this.opts = Object.assign({
      reviver: void 0,
      maxAttachments: 10
    }, typeof e == "function" ? { reviver: e } : e);
  }
  /**
   * Decodes an encoded packet string into packet JSON.
   *
   * @param {String} obj - encoded packet
   */
  add(e) {
    let n;
    if (typeof e == "string") {
      if (this.reconstructor)
        throw new Error("got plaintext data when reconstructing a packet");
      n = this.decodeString(e);
      const r = n.type === T.BINARY_EVENT;
      r || n.type === T.BINARY_ACK ? (n.type = r ? T.EVENT : T.ACK, this.reconstructor = new Da(n)) : super.emitReserved("decoded", n);
    } else if (mr(e) || e.base64)
      if (this.reconstructor)
        n = this.reconstructor.takeBinaryData(e), n && (this.reconstructor = null, super.emitReserved("decoded", n));
      else
        throw new Error("got binary data when not reconstructing a packet");
    else
      throw new Error("Unknown type: " + e);
  }
  /**
   * Decode a packet String (JSON data)
   *
   * @param {String} str
   * @return {Object} packet
   */
  decodeString(e) {
    let n = 0;
    const r = {
      type: Number(e.charAt(0))
    };
    if (T[r.type] === void 0)
      throw new Error("unknown packet type " + r.type);
    if (r.type === T.BINARY_EVENT || r.type === T.BINARY_ACK) {
      const i = n + 1;
      for (; e.charAt(++n) !== "-" && n != e.length; )
        ;
      const o = e.substring(i, n);
      if (o != Number(o) || e.charAt(n) !== "-")
        throw new Error("Illegal attachments");
      const a = Number(o);
      if (!Ia(a) || a < 1)
        throw new Error("Illegal attachments");
      if (a > this.opts.maxAttachments)
        throw new Error("too many attachments");
      r.attachments = a;
    }
    if (e.charAt(n + 1) === "/") {
      const i = n + 1;
      for (; ++n && !(e.charAt(n) === "," || n === e.length); )
        ;
      r.nsp = e.substring(i, n);
    } else
      r.nsp = "/";
    const s = e.charAt(n + 1);
    if (s !== "" && Number(s) == s) {
      const i = n + 1;
      for (; ++n; ) {
        const o = e.charAt(n);
        if (o == null || Number(o) != o) {
          --n;
          break;
        }
        if (n === e.length)
          break;
      }
      r.id = Number(e.substring(i, n + 1));
    }
    if (e.charAt(++n)) {
      const i = this.tryParse(e.substr(n));
      if (wr.isPayloadValid(r.type, i))
        r.data = i;
      else
        throw new Error("invalid payload");
    }
    return r;
  }
  tryParse(e) {
    try {
      return JSON.parse(e, this.opts.reviver);
    } catch {
      return !1;
    }
  }
  static isPayloadValid(e, n) {
    switch (e) {
      case T.CONNECT:
        return jr(n);
      case T.DISCONNECT:
        return n === void 0;
      case T.CONNECT_ERROR:
        return typeof n == "string" || jr(n);
      case T.EVENT:
      case T.BINARY_EVENT:
        return Array.isArray(n) && (typeof n[0] == "number" || typeof n[0] == "string" && qa.indexOf(n[0]) === -1);
      case T.ACK:
      case T.BINARY_ACK:
        return Array.isArray(n);
    }
  }
  /**
   * Deallocates a parser's resources
   */
  destroy() {
    this.reconstructor && (this.reconstructor.finishedReconstruction(), this.reconstructor = null);
  }
}
class Da {
  constructor(e) {
    this.packet = e, this.buffers = [], this.reconPack = e;
  }
  /**
   * Method to be called when binary data received from connection
   * after a BINARY_EVENT packet.
   *
   * @param {Buffer | ArrayBuffer} binData - the raw binary data received
   * @return {null | Object} returns null if more binary data is expected or
   *   a reconstructed packet object if all buffers have been received.
   */
  takeBinaryData(e) {
    if (this.buffers.push(e), this.buffers.length === this.reconPack.attachments) {
      const n = La(this.reconPack, this.buffers);
      return this.finishedReconstruction(), n;
    }
    return null;
  }
  /**
   * Cleans up binary packet reconstruction variables.
   */
  finishedReconstruction() {
    this.reconPack = null, this.buffers = [];
  }
}
const Ia = Number.isInteger || function(t) {
  return typeof t == "number" && isFinite(t) && Math.floor(t) === t;
};
function jr(t) {
  return Object.prototype.toString.call(t) === "[object Object]";
}
const Ma = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  Decoder: wr,
  Encoder: Pa,
  get PacketType() {
    return T;
  }
}, Symbol.toStringTag, { value: "Module" }));
function ce(t, e, n) {
  return t.on(e, n), function() {
    t.off(e, n);
  };
}
const Fa = Object.freeze({
  connect: 1,
  connect_error: 1,
  disconnect: 1,
  disconnecting: 1,
  // EventEmitter reserved events: https://nodejs.org/api/events.html#events_event_newlistener
  newListener: 1,
  removeListener: 1
});
class oi extends P {
  /**
   * `Socket` constructor.
   */
  constructor(e, n, r) {
    super(), this.connected = !1, this.recovered = !1, this.receiveBuffer = [], this.sendBuffer = [], this._queue = [], this._queueSeq = 0, this.ids = 0, this.acks = {}, this.flags = {}, this.io = e, this.nsp = n, r && r.auth && (this.auth = r.auth), this._opts = Object.assign({}, r), this.io._autoConnect && this.open();
  }
  /**
   * Whether the socket is currently disconnected
   *
   * @example
   * const socket = io();
   *
   * socket.on("connect", () => {
   *   console.log(socket.disconnected); // false
   * });
   *
   * socket.on("disconnect", () => {
   *   console.log(socket.disconnected); // true
   * });
   */
  get disconnected() {
    return !this.connected;
  }
  /**
   * Subscribe to open, close and packet events
   *
   * @private
   */
  subEvents() {
    if (this.subs)
      return;
    const e = this.io;
    this.subs = [
      ce(e, "open", this.onopen.bind(this)),
      ce(e, "packet", this.onpacket.bind(this)),
      ce(e, "error", this.onerror.bind(this)),
      ce(e, "close", this.onclose.bind(this))
    ];
  }
  /**
   * Whether the Socket will try to reconnect when its Manager connects or reconnects.
   *
   * @example
   * const socket = io();
   *
   * console.log(socket.active); // true
   *
   * socket.on("disconnect", (reason) => {
   *   if (reason === "io server disconnect") {
   *     // the disconnection was initiated by the server, you need to manually reconnect
   *     console.log(socket.active); // false
   *   }
   *   // else the socket will automatically try to reconnect
   *   console.log(socket.active); // true
   * });
   */
  get active() {
    return !!this.subs;
  }
  /**
   * "Opens" the socket.
   *
   * @example
   * const socket = io({
   *   autoConnect: false
   * });
   *
   * socket.connect();
   */
  connect() {
    return this.connected ? this : (this.subEvents(), this.io._reconnecting || this.io.open(), this.io._readyState === "open" && this.onopen(), this);
  }
  /**
   * Alias for {@link connect()}.
   */
  open() {
    return this.connect();
  }
  /**
   * Sends a `message` event.
   *
   * This method mimics the WebSocket.send() method.
   *
   * @see https://developer.mozilla.org/en-US/docs/Web/API/WebSocket/send
   *
   * @example
   * socket.send("hello");
   *
   * // this is equivalent to
   * socket.emit("message", "hello");
   *
   * @return self
   */
  send(...e) {
    return e.unshift("message"), this.emit.apply(this, e), this;
  }
  /**
   * Override `emit`.
   * If the event is in `events`, it's emitted normally.
   *
   * @example
   * socket.emit("hello", "world");
   *
   * // all serializable datastructures are supported (no need to call JSON.stringify)
   * socket.emit("hello", 1, "2", { 3: ["4"], 5: Uint8Array.from([6]) });
   *
   * // with an acknowledgement from the server
   * socket.emit("hello", "world", (val) => {
   *   // ...
   * });
   *
   * @return self
   */
  emit(e, ...n) {
    var r, s, i;
    if (Fa.hasOwnProperty(e))
      throw new Error('"' + e.toString() + '" is a reserved event name');
    if (n.unshift(e), this._opts.retries && !this.flags.fromQueue && !this.flags.volatile)
      return this._addToQueue(n), this;
    const o = {
      type: T.EVENT,
      data: n
    };
    if (o.options = {}, o.options.compress = this.flags.compress !== !1, typeof n[n.length - 1] == "function") {
      const h = this.ids++, p = n.pop();
      this._registerAckCallback(h, p), o.id = h;
    }
    const a = (s = (r = this.io.engine) === null || r === void 0 ? void 0 : r.transport) === null || s === void 0 ? void 0 : s.writable, l = this.connected && !(!((i = this.io.engine) === null || i === void 0) && i._hasPingExpired());
    return this.flags.volatile && !a || (l ? (this.notifyOutgoingListeners(o), this.packet(o)) : this.sendBuffer.push(o)), this.flags = {}, this;
  }
  /**
   * @private
   */
  _registerAckCallback(e, n) {
    var r;
    const s = (r = this.flags.timeout) !== null && r !== void 0 ? r : this._opts.ackTimeout;
    if (s === void 0) {
      this.acks[e] = n;
      return;
    }
    const i = this.io.setTimeoutFn(() => {
      delete this.acks[e];
      for (let a = 0; a < this.sendBuffer.length; a++)
        this.sendBuffer[a].id === e && this.sendBuffer.splice(a, 1);
      n.call(this, new Error("operation has timed out"));
    }, s), o = (...a) => {
      this.io.clearTimeoutFn(i), n.apply(this, a);
    };
    o.withError = !0, this.acks[e] = o;
  }
  /**
   * Emits an event and waits for an acknowledgement
   *
   * @example
   * // without timeout
   * const response = await socket.emitWithAck("hello", "world");
   *
   * // with a specific timeout
   * try {
   *   const response = await socket.timeout(1000).emitWithAck("hello", "world");
   * } catch (err) {
   *   // the server did not acknowledge the event in the given delay
   * }
   *
   * @return a Promise that will be fulfilled when the server acknowledges the event
   */
  emitWithAck(e, ...n) {
    return new Promise((r, s) => {
      const i = (o, a) => o ? s(o) : r(a);
      i.withError = !0, n.push(i), this.emit(e, ...n);
    });
  }
  /**
   * Add the packet to the queue.
   * @param args
   * @private
   */
  _addToQueue(e) {
    let n;
    typeof e[e.length - 1] == "function" && (n = e.pop());
    const r = {
      id: this._queueSeq++,
      tryCount: 0,
      pending: !1,
      args: e,
      flags: Object.assign({ fromQueue: !0 }, this.flags)
    };
    e.push((s, ...i) => (this._queue[0], s !== null ? r.tryCount > this._opts.retries && (this._queue.shift(), n && n(s)) : (this._queue.shift(), n && n(null, ...i)), r.pending = !1, this._drainQueue())), this._queue.push(r), this._drainQueue();
  }
  /**
   * Send the first packet of the queue, and wait for an acknowledgement from the server.
   * @param force - whether to resend a packet that has not been acknowledged yet
   *
   * @private
   */
  _drainQueue(e = !1) {
    if (!this.connected || this._queue.length === 0)
      return;
    const n = this._queue[0];
    n.pending && !e || (n.pending = !0, n.tryCount++, this.flags = n.flags, this.emit.apply(this, n.args));
  }
  /**
   * Sends a packet.
   *
   * @param packet
   * @private
   */
  packet(e) {
    e.nsp = this.nsp, this.io._packet(e);
  }
  /**
   * Called upon engine `open`.
   *
   * @private
   */
  onopen() {
    typeof this.auth == "function" ? this.auth((e) => {
      this._sendConnectPacket(e);
    }) : this._sendConnectPacket(this.auth);
  }
  /**
   * Sends a CONNECT packet to initiate the Socket.IO session.
   *
   * @param data
   * @private
   */
  _sendConnectPacket(e) {
    this.packet({
      type: T.CONNECT,
      data: this._pid ? Object.assign({ pid: this._pid, offset: this._lastOffset }, e) : e
    });
  }
  /**
   * Called upon engine or manager `error`.
   *
   * @param err
   * @private
   */
  onerror(e) {
    this.connected || this.emitReserved("connect_error", e);
  }
  /**
   * Called upon engine `close`.
   *
   * @param reason
   * @param description
   * @private
   */
  onclose(e, n) {
    this.connected = !1, delete this.id, this.emitReserved("disconnect", e, n), this._clearAcks();
  }
  /**
   * Clears the acknowledgement handlers upon disconnection, since the client will never receive an acknowledgement from
   * the server.
   *
   * @private
   */
  _clearAcks() {
    Object.keys(this.acks).forEach((e) => {
      if (!this.sendBuffer.some((r) => String(r.id) === e)) {
        const r = this.acks[e];
        delete this.acks[e], r.withError && r.call(this, new Error("socket has been disconnected"));
      }
    });
  }
  /**
   * Called with socket packet.
   *
   * @param packet
   * @private
   */
  onpacket(e) {
    if (e.nsp === this.nsp)
      switch (e.type) {
        case T.CONNECT:
          e.data && e.data.sid ? this.onconnect(e.data.sid, e.data.pid) : this.emitReserved("connect_error", new Error("It seems you are trying to reach a Socket.IO server in v2.x with a v3.x client, but they are not compatible (more information here: https://socket.io/docs/v3/migrating-from-2-x-to-3-0/)"));
          break;
        case T.EVENT:
        case T.BINARY_EVENT:
          this.onevent(e);
          break;
        case T.ACK:
        case T.BINARY_ACK:
          this.onack(e);
          break;
        case T.DISCONNECT:
          this.ondisconnect();
          break;
        case T.CONNECT_ERROR:
          this.destroy();
          const r = new Error(e.data.message);
          r.data = e.data.data, this.emitReserved("connect_error", r);
          break;
      }
  }
  /**
   * Called upon a server event.
   *
   * @param packet
   * @private
   */
  onevent(e) {
    const n = e.data || [];
    e.id != null && n.push(this.ack(e.id)), this.connected ? this.emitEvent(n) : this.receiveBuffer.push(Object.freeze(n));
  }
  emitEvent(e) {
    if (this._anyListeners && this._anyListeners.length) {
      const n = this._anyListeners.slice();
      for (const r of n)
        r.apply(this, e);
    }
    super.emit.apply(this, e), this._pid && e.length && typeof e[e.length - 1] == "string" && (this._lastOffset = e[e.length - 1]);
  }
  /**
   * Produces an ack callback to emit with an event.
   *
   * @private
   */
  ack(e) {
    const n = this;
    let r = !1;
    return function(...s) {
      r || (r = !0, n.packet({
        type: T.ACK,
        id: e,
        data: s
      }));
    };
  }
  /**
   * Called upon a server acknowledgement.
   *
   * @param packet
   * @private
   */
  onack(e) {
    const n = this.acks[e.id];
    typeof n == "function" && (delete this.acks[e.id], n.withError && e.data.unshift(null), n.apply(this, e.data));
  }
  /**
   * Called upon server connect.
   *
   * @private
   */
  onconnect(e, n) {
    this.id = e, this.recovered = n && this._pid === n, this._pid = n, this.connected = !0, this.emitBuffered(), this._drainQueue(!0), this.emitReserved("connect");
  }
  /**
   * Emit buffered events (received and emitted).
   *
   * @private
   */
  emitBuffered() {
    this.receiveBuffer.forEach((e) => this.emitEvent(e)), this.receiveBuffer = [], this.sendBuffer.forEach((e) => {
      this.notifyOutgoingListeners(e), this.packet(e);
    }), this.sendBuffer = [];
  }
  /**
   * Called upon server disconnect.
   *
   * @private
   */
  ondisconnect() {
    this.destroy(), this.onclose("io server disconnect");
  }
  /**
   * Called upon forced client/server side disconnections,
   * this method ensures the manager stops tracking us and
   * that reconnections don't get triggered for this.
   *
   * @private
   */
  destroy() {
    this.subs && (this.subs.forEach((e) => e()), this.subs = void 0), this.io._destroy(this);
  }
  /**
   * Disconnects the socket manually. In that case, the socket will not try to reconnect.
   *
   * If this is the last active Socket instance of the {@link Manager}, the low-level connection will be closed.
   *
   * @example
   * const socket = io();
   *
   * socket.on("disconnect", (reason) => {
   *   // console.log(reason); prints "io client disconnect"
   * });
   *
   * socket.disconnect();
   *
   * @return self
   */
  disconnect() {
    return this.connected && this.packet({ type: T.DISCONNECT }), this.destroy(), this.connected && this.onclose("io client disconnect"), this;
  }
  /**
   * Alias for {@link disconnect()}.
   *
   * @return self
   */
  close() {
    return this.disconnect();
  }
  /**
   * Sets the compress flag.
   *
   * @example
   * socket.compress(false).emit("hello");
   *
   * @param compress - if `true`, compresses the sending data
   * @return self
   */
  compress(e) {
    return this.flags.compress = e, this;
  }
  /**
   * Sets a modifier for a subsequent event emission that the event message will be dropped when this socket is not
   * ready to send messages.
   *
   * @example
   * socket.volatile.emit("hello"); // the server may or may not receive it
   *
   * @returns self
   */
  get volatile() {
    return this.flags.volatile = !0, this;
  }
  /**
   * Sets a modifier for a subsequent event emission that the callback will be called with an error when the
   * given number of milliseconds have elapsed without an acknowledgement from the server:
   *
   * @example
   * socket.timeout(5000).emit("my-event", (err) => {
   *   if (err) {
   *     // the server did not acknowledge the event in the given delay
   *   }
   * });
   *
   * @returns self
   */
  timeout(e) {
    return this.flags.timeout = e, this;
  }
  /**
   * Adds a listener that will be fired when any event is emitted. The event name is passed as the first argument to the
   * callback.
   *
   * @example
   * socket.onAny((event, ...args) => {
   *   console.log(`got ${event}`);
   * });
   *
   * @param listener
   */
  onAny(e) {
    return this._anyListeners = this._anyListeners || [], this._anyListeners.push(e), this;
  }
  /**
   * Adds a listener that will be fired when any event is emitted. The event name is passed as the first argument to the
   * callback. The listener is added to the beginning of the listeners array.
   *
   * @example
   * socket.prependAny((event, ...args) => {
   *   console.log(`got event ${event}`);
   * });
   *
   * @param listener
   */
  prependAny(e) {
    return this._anyListeners = this._anyListeners || [], this._anyListeners.unshift(e), this;
  }
  /**
   * Removes the listener that will be fired when any event is emitted.
   *
   * @example
   * const catchAllListener = (event, ...args) => {
   *   console.log(`got event ${event}`);
   * }
   *
   * socket.onAny(catchAllListener);
   *
   * // remove a specific listener
   * socket.offAny(catchAllListener);
   *
   * // or remove all listeners
   * socket.offAny();
   *
   * @param listener
   */
  offAny(e) {
    if (!this._anyListeners)
      return this;
    if (e) {
      const n = this._anyListeners;
      for (let r = 0; r < n.length; r++)
        if (e === n[r])
          return n.splice(r, 1), this;
    } else
      this._anyListeners = [];
    return this;
  }
  /**
   * Returns an array of listeners that are listening for any event that is specified. This array can be manipulated,
   * e.g. to remove listeners.
   */
  listenersAny() {
    return this._anyListeners || [];
  }
  /**
   * Adds a listener that will be fired when any event is emitted. The event name is passed as the first argument to the
   * callback.
   *
   * Note: acknowledgements sent to the server are not included.
   *
   * @example
   * socket.onAnyOutgoing((event, ...args) => {
   *   console.log(`sent event ${event}`);
   * });
   *
   * @param listener
   */
  onAnyOutgoing(e) {
    return this._anyOutgoingListeners = this._anyOutgoingListeners || [], this._anyOutgoingListeners.push(e), this;
  }
  /**
   * Adds a listener that will be fired when any event is emitted. The event name is passed as the first argument to the
   * callback. The listener is added to the beginning of the listeners array.
   *
   * Note: acknowledgements sent to the server are not included.
   *
   * @example
   * socket.prependAnyOutgoing((event, ...args) => {
   *   console.log(`sent event ${event}`);
   * });
   *
   * @param listener
   */
  prependAnyOutgoing(e) {
    return this._anyOutgoingListeners = this._anyOutgoingListeners || [], this._anyOutgoingListeners.unshift(e), this;
  }
  /**
   * Removes the listener that will be fired when any event is emitted.
   *
   * @example
   * const catchAllListener = (event, ...args) => {
   *   console.log(`sent event ${event}`);
   * }
   *
   * socket.onAnyOutgoing(catchAllListener);
   *
   * // remove a specific listener
   * socket.offAnyOutgoing(catchAllListener);
   *
   * // or remove all listeners
   * socket.offAnyOutgoing();
   *
   * @param [listener] - the catch-all listener (optional)
   */
  offAnyOutgoing(e) {
    if (!this._anyOutgoingListeners)
      return this;
    if (e) {
      const n = this._anyOutgoingListeners;
      for (let r = 0; r < n.length; r++)
        if (e === n[r])
          return n.splice(r, 1), this;
    } else
      this._anyOutgoingListeners = [];
    return this;
  }
  /**
   * Returns an array of listeners that are listening for any event that is specified. This array can be manipulated,
   * e.g. to remove listeners.
   */
  listenersAnyOutgoing() {
    return this._anyOutgoingListeners || [];
  }
  /**
   * Notify the listeners for each packet sent
   *
   * @param packet
   *
   * @private
   */
  notifyOutgoingListeners(e) {
    if (this._anyOutgoingListeners && this._anyOutgoingListeners.length) {
      const n = this._anyOutgoingListeners.slice();
      for (const r of n)
        r.apply(this, e.data);
    }
  }
}
function Tt(t) {
  t = t || {}, this.ms = t.min || 100, this.max = t.max || 1e4, this.factor = t.factor || 2, this.jitter = t.jitter > 0 && t.jitter <= 1 ? t.jitter : 0, this.attempts = 0;
}
Tt.prototype.duration = function() {
  var t = this.ms * Math.pow(this.factor, this.attempts++);
  if (this.jitter) {
    var e = Math.random(), n = Math.floor(e * this.jitter * t);
    t = (Math.floor(e * 10) & 1) == 0 ? t - n : t + n;
  }
  return Math.min(t, this.max) | 0;
};
Tt.prototype.reset = function() {
  this.attempts = 0;
};
Tt.prototype.setMin = function(t) {
  this.ms = t;
};
Tt.prototype.setMax = function(t) {
  this.max = t;
};
Tt.prototype.setJitter = function(t) {
  this.jitter = t;
};
class sr extends P {
  constructor(e, n) {
    var r;
    super(), this.nsps = {}, this.subs = [], e && typeof e == "object" && (n = e, e = void 0), n = n || {}, n.path = n.path || "/socket.io", this.opts = n, On(this, n), this.reconnection(n.reconnection !== !1), this.reconnectionAttempts(n.reconnectionAttempts || 1 / 0), this.reconnectionDelay(n.reconnectionDelay || 1e3), this.reconnectionDelayMax(n.reconnectionDelayMax || 5e3), this.randomizationFactor((r = n.randomizationFactor) !== null && r !== void 0 ? r : 0.5), this.backoff = new Tt({
      min: this.reconnectionDelay(),
      max: this.reconnectionDelayMax(),
      jitter: this.randomizationFactor()
    }), this.timeout(n.timeout == null ? 2e4 : n.timeout), this._readyState = "closed", this.uri = e;
    const s = n.parser || Ma;
    this.encoder = new s.Encoder(), this.decoder = new s.Decoder(), this._autoConnect = n.autoConnect !== !1, this._autoConnect && this.open();
  }
  reconnection(e) {
    return arguments.length ? (this._reconnection = !!e, e || (this.skipReconnect = !0), this) : this._reconnection;
  }
  reconnectionAttempts(e) {
    return e === void 0 ? this._reconnectionAttempts : (this._reconnectionAttempts = e, this);
  }
  reconnectionDelay(e) {
    var n;
    return e === void 0 ? this._reconnectionDelay : (this._reconnectionDelay = e, (n = this.backoff) === null || n === void 0 || n.setMin(e), this);
  }
  randomizationFactor(e) {
    var n;
    return e === void 0 ? this._randomizationFactor : (this._randomizationFactor = e, (n = this.backoff) === null || n === void 0 || n.setJitter(e), this);
  }
  reconnectionDelayMax(e) {
    var n;
    return e === void 0 ? this._reconnectionDelayMax : (this._reconnectionDelayMax = e, (n = this.backoff) === null || n === void 0 || n.setMax(e), this);
  }
  timeout(e) {
    return arguments.length ? (this._timeout = e, this) : this._timeout;
  }
  /**
   * Starts trying to reconnect if reconnection is enabled and we have not
   * started reconnecting yet
   *
   * @private
   */
  maybeReconnectOnOpen() {
    !this._reconnecting && this._reconnection && this.backoff.attempts === 0 && this.reconnect();
  }
  /**
   * Sets the current transport `socket`.
   *
   * @param {Function} fn - optional, callback
   * @return self
   * @public
   */
  open(e) {
    if (~this._readyState.indexOf("open"))
      return this;
    this.engine = new Aa(this.uri, this.opts);
    const n = this.engine, r = this;
    this._readyState = "opening", this.skipReconnect = !1;
    const s = ce(n, "open", function() {
      r.onopen(), e && e();
    }), i = (a) => {
      this.cleanup(), this._readyState = "closed", this.emitReserved("error", a), e ? e(a) : this.maybeReconnectOnOpen();
    }, o = ce(n, "error", i);
    if (this._timeout !== !1) {
      const a = this._timeout, l = this.setTimeoutFn(() => {
        s(), i(new Error("timeout")), n.close();
      }, a);
      this.opts.autoUnref && l.unref(), this.subs.push(() => {
        this.clearTimeoutFn(l);
      });
    }
    return this.subs.push(s), this.subs.push(o), this;
  }
  /**
   * Alias for open()
   *
   * @return self
   * @public
   */
  connect(e) {
    return this.open(e);
  }
  /**
   * Called upon transport open.
   *
   * @private
   */
  onopen() {
    this.cleanup(), this._readyState = "open", this.emitReserved("open");
    const e = this.engine;
    this.subs.push(
      ce(e, "ping", this.onping.bind(this)),
      ce(e, "data", this.ondata.bind(this)),
      ce(e, "error", this.onerror.bind(this)),
      ce(e, "close", this.onclose.bind(this)),
      // @ts-ignore
      ce(this.decoder, "decoded", this.ondecoded.bind(this))
    );
  }
  /**
   * Called upon a ping.
   *
   * @private
   */
  onping() {
    this.emitReserved("ping");
  }
  /**
   * Called with data.
   *
   * @private
   */
  ondata(e) {
    try {
      this.decoder.add(e);
    } catch (n) {
      this.onclose("parse error", n);
    }
  }
  /**
   * Called when parser fully decodes a packet.
   *
   * @private
   */
  ondecoded(e) {
    An(() => {
      this.emitReserved("packet", e);
    }, this.setTimeoutFn);
  }
  /**
   * Called upon socket error.
   *
   * @private
   */
  onerror(e) {
    this.emitReserved("error", e);
  }
  /**
   * Creates a new socket for the given `nsp`.
   *
   * @return {Socket}
   * @public
   */
  socket(e, n) {
    let r = this.nsps[e];
    return r ? this._autoConnect && !r.active && r.connect() : (r = new oi(this, e, n), this.nsps[e] = r), r;
  }
  /**
   * Called upon a socket close.
   *
   * @param socket
   * @private
   */
  _destroy(e) {
    const n = Object.keys(this.nsps);
    for (const r of n)
      if (this.nsps[r].active)
        return;
    this._close();
  }
  /**
   * Writes a packet.
   *
   * @param packet
   * @private
   */
  _packet(e) {
    const n = this.encoder.encode(e);
    for (let r = 0; r < n.length; r++)
      this.engine.write(n[r], e.options);
  }
  /**
   * Clean up transport subscriptions and packet buffer.
   *
   * @private
   */
  cleanup() {
    this.subs.forEach((e) => e()), this.subs.length = 0, this.decoder.destroy();
  }
  /**
   * Close the current socket.
   *
   * @private
   */
  _close() {
    this.skipReconnect = !0, this._reconnecting = !1, this.onclose("forced close");
  }
  /**
   * Alias for close()
   *
   * @private
   */
  disconnect() {
    return this._close();
  }
  /**
   * Called when:
   *
   * - the low-level engine is closed
   * - the parser encountered a badly formatted packet
   * - all sockets are disconnected
   *
   * @private
   */
  onclose(e, n) {
    var r;
    this.cleanup(), (r = this.engine) === null || r === void 0 || r.close(), this.backoff.reset(), this._readyState = "closed", this.emitReserved("close", e, n), this._reconnection && !this.skipReconnect && this.reconnect();
  }
  /**
   * Attempt a reconnection.
   *
   * @private
   */
  reconnect() {
    if (this._reconnecting || this.skipReconnect)
      return this;
    const e = this;
    if (this.backoff.attempts >= this._reconnectionAttempts)
      this.backoff.reset(), this.emitReserved("reconnect_failed"), this._reconnecting = !1;
    else {
      const n = this.backoff.duration();
      this._reconnecting = !0;
      const r = this.setTimeoutFn(() => {
        e.skipReconnect || (this.emitReserved("reconnect_attempt", e.backoff.attempts), !e.skipReconnect && e.open((s) => {
          s ? (e._reconnecting = !1, e.reconnect(), this.emitReserved("reconnect_error", s)) : e.onreconnect();
        }));
      }, n);
      this.opts.autoUnref && r.unref(), this.subs.push(() => {
        this.clearTimeoutFn(r);
      });
    }
  }
  /**
   * Called upon successful reconnect.
   *
   * @private
   */
  onreconnect() {
    const e = this.backoff.attempts;
    this._reconnecting = !1, this.backoff.reset(), this.emitReserved("reconnect", e);
  }
}
const St = {};
function cn(t, e) {
  typeof t == "object" && (e = t, t = void 0), e = e || {};
  const n = Oa(t, e.path || "/socket.io"), r = n.source, s = n.id, i = n.path, o = St[s] && i in St[s].nsps, a = e.forceNew || e["force new connection"] || e.multiplex === !1 || o;
  let l;
  return a ? l = new sr(r, e) : (St[s] || (St[s] = new sr(r, e)), l = St[s]), n.query && !e.query && (e.query = n.queryKey), l.socket(n.path, e);
}
Object.assign(cn, {
  Manager: sr,
  Socket: oi,
  io: cn,
  connect: cn
});
var Ua = /* @__PURE__ */ Hs('<p class="error svelte-l53qdy" role="alert"> </p>'), Va = /* @__PURE__ */ Hs('<section class="reference-status svelte-l53qdy" data-testid="reference-status"><header class="svelte-l53qdy"><h2 class="svelte-l53qdy">Reference Mod</h2> <span> </span></header> <dl class="state svelte-l53qdy"><dt class="svelte-l53qdy">task_id</dt> <dd class="svelte-l53qdy"> </dd> <dt class="svelte-l53qdy">status</dt> <dd class="svelte-l53qdy"> </dd> <dt class="svelte-l53qdy">count</dt> <dd class="svelte-l53qdy"> </dd></dl> <button class="svelte-l53qdy"> </button> <!> <footer class="svelte-l53qdy"> </footer></section>');
const Ha = {
  hash: "svelte-l53qdy",
  code: ".reference-status.svelte-l53qdy {font-family:system-ui, sans-serif;color:#e5e7eb;background:#111827;border:1px solid #374151;border-radius:8px;padding:1rem 1.25rem;max-width:22rem;display:flex;flex-direction:column;gap:0.75rem;}header.svelte-l53qdy {display:flex;align-items:center;justify-content:space-between;}h2.svelte-l53qdy {margin:0;font-size:1rem;font-weight:600;}.conn.svelte-l53qdy {font-size:0.75rem;padding:0.1rem 0.5rem;border-radius:999px;background:#374151;color:#9ca3af;}.conn.online.svelte-l53qdy {background:#064e3b;color:#34d399;}dl.state.svelte-l53qdy {display:grid;grid-template-columns:auto 1fr;gap:0.25rem 0.75rem;margin:0;font-size:0.875rem;}dt.svelte-l53qdy {color:#9ca3af;}dd.svelte-l53qdy {margin:0;font-variant-numeric:tabular-nums;word-break:break-all;}button.svelte-l53qdy {appearance:none;border:0;border-radius:6px;padding:0.5rem 0.75rem;background:#2563eb;color:#fff;font-size:0.875rem;font-weight:500;cursor:pointer;}button.svelte-l53qdy:disabled {opacity:0.6;cursor:default;}.error.svelte-l53qdy {margin:0;font-size:0.8rem;color:#fca5a5;}footer.svelte-l53qdy {font-size:0.75rem;color:#6b7280;}"
};
function ja(t, e) {
  rs(e, !0), Po(t, Ha);
  let n = Dn(e, "authToken", 7, ""), r = Dn(e, "apiBase", 7, ""), s = Dn(e, "currentUser", 7, null), i = /* @__PURE__ */ Q(null), o = /* @__PURE__ */ Q(0), a = /* @__PURE__ */ Q(!1), l = /* @__PURE__ */ Q(!1), c = /* @__PURE__ */ Q(""), h;
  function p() {
    const g = (r() || "").replace(/\/api\/v1\/?$/, "");
    return g ? g.replace(/\/$/, "") : typeof location < "u" ? location.origin : "";
  }
  function u(g) {
    g && (R(i, g.latest ?? null, !0), R(o, typeof g.count == "number" ? g.count : 0, !0));
  }
  async function d() {
    try {
      const g = await fetch(`${p()}/reference/state`, { headers: { Authorization: `Bearer ${n()}` } });
      if (!g.ok) {
        R(c, `state request failed (${g.status})`);
        return;
      }
      u(await g.json()), R(c, "");
    } catch (g) {
      R(c, `state request error: ${g instanceof Error ? g.message : String(g)}`);
    }
  }
  function _() {
    h = cn(`${p()}/reference`, {
      path: "/ws/socket.io",
      auth: { token: n() },
      transports: ["websocket", "polling"]
    }), h.on("connect", () => {
      R(a, !0), h.emit("sync");
    }), h.on("disconnect", () => {
      R(a, !1);
    }), h.on("reference:state", (g) => u(g)), h.on("connect_error", (g) => {
      R(c, `socket error: ${g instanceof Error ? g.message : String(g)}`);
    });
  }
  async function v() {
    R(l, !0), R(c, "");
    try {
      const g = await fetch(`${p()}/reference/submit`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${n()}`,
          "Content-Type": "application/json"
        },
        body: "{}"
      });
      g.ok ? await d() : R(c, `submit failed (${g.status}) --- no direct tool route yet; see T-C08 finding`);
    } catch (g) {
      R(c, `submit error: ${g instanceof Error ? g.message : String(g)}`);
    } finally {
      R(l, !1);
    }
  }
  zs(() => {
    d(), _();
  }), Lo(() => {
    try {
      h == null || h.disconnect();
    } catch {
    }
  });
  const S = /* @__PURE__ */ no(() => {
    var g, Oe, Wt;
    return ((g = s()) == null ? void 0 : g.name) || ((Oe = s()) == null ? void 0 : Oe.email) || ((Wt = s()) == null ? void 0 : Wt.id) || "unknown";
  });
  var L = {
    get authToken() {
      return n();
    },
    set authToken(g = "") {
      n(g), nn();
    },
    get apiBase() {
      return r();
    },
    set apiBase(g = "") {
      r(g), nn();
    },
    get currentUser() {
      return s();
    },
    set currentUser(g = null) {
      s(g), nn();
    }
  }, _e = Va(), ve = ge(_e), Kt = Pe(ge(ve), 2);
  let br;
  var ai = ge(Kt, !0);
  ye(Kt), ye(ve);
  var Rn = Pe(ve, 2), xn = Pe(ge(Rn), 2), fi = ge(xn, !0);
  ye(xn);
  var Cn = Pe(xn, 4), li = ge(Cn, !0);
  ye(Cn);
  var Er = Pe(Cn, 4), ci = ge(Er, !0);
  ye(Er), ye(Rn);
  var $t = Pe(Rn, 2), ui = ge($t, !0);
  ye($t);
  var kr = Pe($t, 2);
  {
    var hi = (g) => {
      var Oe = Ua(), Wt = ge(Oe, !0);
      ye(Oe), Nr(() => je(Wt, x(c))), Gn(g, Oe);
    };
    qo(kr, (g) => {
      x(c) && g(hi);
    });
  }
  var Tr = Pe(kr, 2), di = ge(Tr);
  return ye(Tr), ye(_e), Nr(() => {
    var g, Oe;
    br = Io(Kt, 1, "conn svelte-l53qdy", null, br, { online: x(a) }), Uo(Kt, "title", x(a) ? "subscribed" : "not subscribed"), je(ai, x(a) ? "live" : "offline"), je(fi, ((g = x(i)) == null ? void 0 : g.task_id) ?? "—"), je(li, ((Oe = x(i)) == null ? void 0 : Oe.status) ?? "—"), je(ci, x(o)), $t.disabled = x(l), je(ui, x(l) ? "Submitting…" : "Submit work"), je(di, `as ${x(S) ?? ""}`);
  }), $o("click", $t, v), Gn(t, _e), ss(L);
}
So(["click"]);
customElements.define("mod-reference", Ko(ja, { authToken: {}, apiBase: {}, currentUser: {} }, [], [], { mode: "open" }));
