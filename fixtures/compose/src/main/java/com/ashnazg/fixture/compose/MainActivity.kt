package com.ashnazg.fixture.compose

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp

@OptIn(ExperimentalComposeUiApi::class)
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    var count by remember { mutableIntStateOf(0) }
                    var input by remember { mutableStateOf("") }
                    Column(
                        modifier = Modifier
                            .fillMaxSize()
                            .semantics { testTagsAsResourceId = true }
                            .padding(24.dp),
                        verticalArrangement = Arrangement.spacedBy(20.dp),
                    ) {
                        Button(
                            onClick = { count += 1 },
                            modifier = Modifier.testTag("compose-button"),
                        ) {
                            Text("Compose count: $count")
                        }
                        BasicTextField(
                            value = input,
                            onValueChange = { input = it },
                            modifier = Modifier
                                .testTag("compose-input")
                                .semantics { contentDescription = "Compose input" },
                            decorationBox = { innerTextField ->
                                if (input.isEmpty()) Text("Compose input")
                                innerTextField()
                            },
                        )
                    }
                }
            }
        }
    }
}
