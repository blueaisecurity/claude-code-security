const { test } = require("node:test");
const assert = require("node:assert");
const { total } = require("../src/cart");

test("total sums item prices", () => {
  assert.strictEqual(total([{ price: 2 }, { price: 3 }]), 5);
});

test("total applies the discount rate", () => {
  assert.strictEqual(total([{ price: 10 }], 0.1), 9);
});
