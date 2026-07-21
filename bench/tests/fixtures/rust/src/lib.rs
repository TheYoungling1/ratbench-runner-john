pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a() {
        assert_eq!(add(1, 2), 3);
    }

    #[test]
    fn b() {
        assert_eq!(add(2, 2), 4);
    }

    #[test]
    fn c() {
        assert_eq!(add(1, 1), 3); // intentional failure
    }
}
