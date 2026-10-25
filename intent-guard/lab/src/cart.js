// Demo module for the intent guard lab. Fake, not published.
function total(items, discount = 0) {
  const sum = items.reduce((acc, item) => acc + item.price, 0);
  return sum - discount; // bug: treats the discount as a flat amount, not a rate
}

module.exports = { total };
