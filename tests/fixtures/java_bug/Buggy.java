// Buggy Java — NullPointerException
public class Buggy {
    public static double calculateRatio(Integer numerator, Integer denominator) {
        if (denominator == 0) {
            return 0;
        }
        return numerator / denominator;  // BUG: numerator may be null → NullPointerException
    }

    public static void main(String[] args) {
        double result = calculateRatio(null, 2);  // BUG: passing null
        System.out.println("Result: " + result);
    }
}
