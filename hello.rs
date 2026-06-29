use rand::seq::SliceRandom;

fn mark_six() {
    let mut rng = rand::thread_rng();
    
    // 1. 用 u8 儲存 1 到 49，並用 collect::<Vec<_>>() 讓 Rust 自己搞定型別
    let mut pool = (1..=49).collect::<Vec<u8>>();
    
    // 2. 隨機選 6 個號碼
    let mut lucky_numbers: Vec<u8> = pool.choose_multiple(&mut rng, 6).cloned().collect();
    
    // 3. 排序
    lucky_numbers.sort();

    println!("六合彩攪珠結果: {:?}", lucky_numbers);
}