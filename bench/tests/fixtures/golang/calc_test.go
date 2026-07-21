package fixture

import "testing"

func TestAddA(t *testing.T) {
	if Add(1, 2) != 3 {
		t.Fatal("1+2 should be 3")
	}
}

func TestAddB(t *testing.T) {
	if Add(2, 2) != 4 {
		t.Fatal("2+2 should be 4")
	}
}

func TestAddC(t *testing.T) {
	if Add(1, 1) != 3 { // intentional failure: 1+1 != 3
		t.Fatal("intentional failure")
	}
}
