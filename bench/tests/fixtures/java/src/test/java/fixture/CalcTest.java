package fixture;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class CalcTest {
    @Test
    void a() {
        assertEquals(3, Calc.add(1, 2));
    }

    @Test
    void b() {
        assertEquals(4, Calc.add(2, 2));
    }

    @Test
    void c() {
        assertEquals(3, Calc.add(1, 1)); // intentional failure
    }
}
