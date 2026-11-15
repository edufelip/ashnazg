import React, {useState} from 'react';
import {
  Pressable,
  SafeAreaView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

export default function App() {
  const [count, setCount] = useState(0);
  const [value, setValue] = useState('');

  return (
    <SafeAreaView style={styles.screen}>
      <View style={styles.content}>
        <Pressable
          accessible
          accessibilityRole="button"
          accessibilityLabel={`React Native count: ${count}`}
          testID="rn-button"
          onPress={() => setCount(current => current + 1)}
          style={styles.button}>
          <Text style={styles.buttonText}>React Native count: {count}</Text>
        </Pressable>
        <TextInput
          accessibilityLabel="React Native input"
          testID="rn-input"
          value={value}
          onChangeText={setValue}
          placeholder="Unicode: café / 東京 / مرحباً"
          placeholderTextColor="#667085"
          style={styles.input}
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: '#f7f8fc',
  },
  content: {
    flex: 1,
    alignItems: 'flex-start',
    padding: 24,
    gap: 20,
  },
  button: {
    minHeight: 52,
    justifyContent: 'center',
    borderRadius: 12,
    backgroundColor: '#315bd6',
    paddingHorizontal: 18,
  },
  buttonText: {
    color: 'white',
    fontSize: 17,
    fontWeight: '600',
  },
  input: {
    minWidth: 280,
    minHeight: 52,
    borderColor: '#98a2b3',
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    color: '#101828',
  },
});
