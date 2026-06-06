// Buggy Rust — unwrap on None
fn calculate_ratio(numerator: Option<i32>, denominator: i32) -> f64 {
    if denominator == 0 {
        return 0.0;
    }
    let num = numerator.unwrap();  // BUG: numerator is None → panic
    num as f64 / denominator as f64
}

fn main() {
    let result = calculate_ratio(None, 2);  // BUG: passing None
    println!("Result: {}", result);
}
