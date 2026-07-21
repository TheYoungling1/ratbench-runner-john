const { add } = require("./sum");

test("a", () => { expect(add(1, 2)).toBe(3); });
test("b", () => { expect(add(2, 2)).toBe(4); });
test("c", () => { expect(add(1, 1)).toBe(3); }); // intentional failure
