package com.example.legacy;

import org.junit.Test;

import java.util.Arrays;

import static org.junit.Assert.assertEquals;

public class LegacyCatalogTest {
    @Test
    public void sortsValues() {
        assertEquals(Arrays.asList("a", "b"), new LegacyCatalog().sorted("b", "a"));
    }
}
