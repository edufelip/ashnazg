package dev.ashnazg.fixture;

import android.app.Activity;
import android.os.Bundle;
import android.text.InputType;
import android.content.Intent;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.util.Log;

public final class MainActivity extends Activity {
    private int count;
    private LinearLayout content;
    private boolean deepLink;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().getDecorView().setSystemUiVisibility(0);
        deepLink = isFixtureDeepLink(getIntent());
        render();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        deepLink = isFixtureDeepLink(intent);
        count = 0;
        render();
    }

    private void render() {
        Log.i("AshnazgFixture", "fixture-opened");
        LinearLayout outer = new LinearLayout(this);
        outer.setOrientation(LinearLayout.VERTICAL);
        outer.setPadding(dp(20), dp(20), dp(20), dp(20));
        TextView title = new TextView(this); title.setText("Ashnazg fixture"); title.setTextSize(22); outer.addView(title);
        if (deepLink) { TextView route = new TextView(this); route.setText("Deep link"); outer.addView(route, matchWrap()); }

        Button increment = new Button(this);
        increment.setId(R.id.increment);
        increment.setAllCaps(false);
        increment.setText("Count: " + count);
        increment.setOnClickListener(view -> { count++; increment.setText("Count: " + count); Log.i("AshnazgFixture", "count=" + count); });
        outer.addView(increment, matchWrap());

        EditText input = new EditText(this);
        input.setId(R.id.input); input.setSingleLine(true); input.setHint("Input"); outer.addView(input, matchWrap());

        EditText password = new EditText(this);
        password.setId(R.id.password); password.setSingleLine(true); password.setHint("Password");
        password.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        password.setText("secret-fixture-sentinel"); outer.addView(password, matchWrap());

        TextView duplicate1 = new TextView(this); duplicate1.setText("Duplicate target"); outer.addView(duplicate1, matchWrap());
        TextView duplicate2 = new TextView(this); duplicate2.setText("Duplicate target"); outer.addView(duplicate2, matchWrap());
        TextView longPress = new TextView(this); longPress.setText("Long press target"); longPress.setPadding(0, dp(14), 0, dp(14));
        longPress.setLongClickable(true);
        longPress.setOnLongClickListener(view -> { ((TextView) view).setText("Long press complete"); return true; });
        outer.addView(longPress, matchWrap());

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(false);
        content = new LinearLayout(this); content.setOrientation(LinearLayout.VERTICAL);
        for (int i = 0; i < 18; i++) {
            TextView filler = new TextView(this); filler.setText("Scrollable row " + i); filler.setPadding(0, dp(14), 0, dp(14));
            content.addView(filler, matchWrap());
        }
        TextView target = new TextView(this); target.setText("Scroll target"); target.setPadding(0, dp(14), 0, dp(14)); content.addView(target, matchWrap());
        scroll.addView(content);
        outer.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(outer);
    }

    private LinearLayout.LayoutParams matchWrap() { return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT); }
    private int dp(int value) { return (int) (value * getResources().getDisplayMetrics().density + .5f); }
    private boolean isFixtureDeepLink(Intent intent) {
        return intent != null && intent.getData() != null && "ashnazg".equals(intent.getData().getScheme()) && "fixture".equals(intent.getData().getHost());
    }
}
